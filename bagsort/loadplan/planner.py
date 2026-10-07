"""配载编排器：把策略层的逐件计划落进只增账本并给操作面回读视图。

职责边界：

* 入场闸（缺秤、单件超限、跨批次重复计入、航班不可配载）在这里判定，缺秤件
  不进策略层，直接给 ``MISSING_WEIGHT`` 结论，交回人工补秤后再提交；
* 策略层 :func:`bagsort.loadplan.policy.plan_batch` 只做纯计算；
* 落账时重算每一步落定后的整舱载重与合重心，并对最终顺序做全前缀复核，
  复核不过则整组回滚（store 会话天然支持），保证账上的顺序永远可行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bagsort.clock import Clock
from bagsort.errors import NotFoundError, OrderingError, ValidationError
from bagsort.flight.book import FlightBook
from bagsort.model.validate import is_open_state
from bagsort.store.store import Store

from .entities import EnvelopeConfig, LoadDecision
from .enums import DecisionKind, OverlimitPolicy, RejectReason
from .ledger import LoadLedger
from .physics import LoadConstraints, PlacedPiece, evaluate, prefixes_hold, total
from .policy import Candidate, PlannedBatch, Step, plan_batch


@dataclass
class PlanDecision:
    """一次提交的可读结论（拒绝理由在此说清楚）。"""

    bag_id: str
    accepted: bool
    kind: str
    reason: str
    reason_text: str
    weight_g: int
    position: int
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "accepted": self.accepted,
            "kind": self.kind,
            "reason": self.reason,
            "reason_text": self.reason_text,
            "weight_g": self.weight_g,
            "position": self.position,
            "detail": self.detail,
        }


REASON_TEXT: dict[RejectReason, str] = {
    RejectReason.MISSING_WEIGHT: "缺称重记录：行李未过秤或秤量未提交，先退回补秤，复核后可重新提交",
    RejectReason.INVALID_WEIGHT: "称重记录无效：重量必须为正数，请复核秤台后重新过秤",
    RejectReason.PIECE_OVERWEIGHT: "单件超重：该件超过本航班单件上限，拒装上转盘，转人工改运",
    RejectReason.DEADLOAD_EXCEEDED: "载重越限：装入该件后货舱业载超过上限",
    RejectReason.CG_OUT_OF_WINDOW: "重心越窗：装入该件后合重心偏出允许窗口",
    RejectReason.ALREADY_COUNTED: "重复计入：该件已在另一批次计入载重，不得重复计重",
    RejectReason.FLIGHT_NOT_PLANNABLE: "航班当前状态不接受配载（未设置载重包线或已关闭）",
    RejectReason.NO_FEASIBLE_SWAP: "越限且无可行换装：没有更重的已确认件可回滚替换，按拒最后一件处理",
    RejectReason.PULLED_OFF_FOR_SWAP: "换装撤下：为给较轻件让位，该已确认件被回滚撤下，交人工改配后续批次",
    RejectReason.PULLED_OFF_RECOUNT: "换装撤下件不得在本航班自动重投：载重占用已释放，请由人工改配后续航班/批次",
}


@dataclass(frozen=True)
class BatchPlanResult:
    flight_id: str
    batch: int
    policy: str
    decisions: list[PlanDecision]
    order: list[str]
    weight_g: int
    headroom_g: int
    cg_cm: float
    cg_window: tuple[float, float]
    rejected: list[str]
    pulled_off: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "batch": self.batch,
            "policy": self.policy,
            "decisions": [decision.as_dict() for decision in self.decisions],
            "order": list(self.order),
            "weight_g": self.weight_g,
            "headroom_g": self.headroom_g,
            "cg_cm": round(self.cg_cm, 3),
            "cg_window": [self.cg_window[0], self.cg_window[1]],
            "rejected": list(self.rejected),
            "pulled_off": list(self.pulled_off),
        }


class LoadPlanner:
    """对外唯一的配载门面。"""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        book: FlightBook,
        ledger: LoadLedger | None = None,
        policy: OverlimitPolicy = OverlimitPolicy.SWAP_LIGHTER,
    ) -> None:
        self._store = store
        self._clock = clock
        self._book = book
        self._ledger = ledger or LoadLedger(store, clock)
        self._policy = policy

    @property
    def ledger(self) -> LoadLedger:
        return self._ledger

    @property
    def policy(self) -> OverlimitPolicy:
        return self._policy

    def set_policy(self, policy: OverlimitPolicy) -> None:
        self._policy = OverlimitPolicy(policy)

    # -- 入场检查 ----------------------------------------------------------

    def _admit(
        self,
        state,
        flight_id: str,
        bag_id: str,
        counted: set[str],
        terminal: set[str],
        pulled_off: set[str],
    ) -> tuple[Candidate | None, RejectReason | None]:
        if bag_id in counted or bag_id in terminal:
            return None, RejectReason.ALREADY_COUNTED
        if bag_id in pulled_off:
            return None, RejectReason.PULLED_OFF_RECOUNT
        record = state.weighings.get(bag_id)
        if record is None:
            return None, RejectReason.MISSING_WEIGHT
        if record.weight_g <= 0:
            return None, RejectReason.INVALID_WEIGHT
        return Candidate(record.bag_id, record.weight_g, record.arm_cm), None

    # -- 单件提交 ----------------------------------------------------------

    def submit(self, bag_id: str, flight_id: str, batch: int = 1) -> PlanDecision:
        """提交一件上转盘，落一笔结论；越限按当前策略处置。"""

        if batch < 0:
            raise ValidationError("batch must not be negative")
        flight = self._book.get(flight_id)
        state = self._store.state()
        envelope = state.envelopes.get(flight_id)
        if not is_open_state(flight.state) or envelope is None:
            self._commit_gate_rejection(
                flight_id, batch, bag_id, RejectReason.FLIGHT_NOT_PLANNABLE
            )
            return self._reject_decision(
                bag_id, RejectReason.FLIGHT_NOT_PLANNABLE, 0, {}
            )
        counted = self._ledger.counted_bag_ids()
        terminal = self._ledger.terminal_bag_ids()
        pulled_off = self._ledger.pulled_off_bag_ids()
        candidate, gate = self._admit(
            state, flight_id, bag_id, counted, terminal, pulled_off
        )
        if candidate is None:
            decision = self._reject_decision(bag_id, gate or RejectReason.MISSING_WEIGHT, 0, {})
            self._commit_gate_rejection(flight_id, batch, bag_id, gate or RejectReason.MISSING_WEIGHT)
            return decision
        constraints = self._to_constraints(envelope)
        plan = plan_batch(
            flight_id,
            batch,
            [candidate],
            constraints,
            policy=self._policy,
        )
        result = self._commit_plan(plan, envelope, constraints)
        return result.decisions[0]

    # -- 整批排程 ----------------------------------------------------------

    def plan(
        self,
        flight_id: str,
        bag_ids: list[str],
        batch: int = 1,
    ) -> BatchPlanResult:
        """按给定顺序对一批行李排装载顺序，逐件落结论。"""

        if batch < 0:
            raise ValidationError("batch must not be negative")
        flight = self._book.get(flight_id)
        state = self._store.state()
        envelope = state.envelopes.get(flight_id)
        plannable = is_open_state(flight.state) and envelope is not None
        if not plannable:
            reason = RejectReason.FLIGHT_NOT_PLANNABLE
            for bag_id in bag_ids:
                self._commit_gate_rejection(flight_id, batch, bag_id, reason)
            return self._empty_result(flight_id, batch, bag_ids, reason)

        constraints = self._to_constraints(envelope)
        counted = self._ledger.counted_bag_ids()
        terminal = self._ledger.terminal_bag_ids()
        pulled_off = self._ledger.pulled_off_bag_ids()
        candidates: list[Candidate] = []
        gate_rejections: list[tuple[str, RejectReason]] = []
        for bag_id in bag_ids:
            candidate, gate = self._admit(
                self._store.state(), flight_id, bag_id, counted, terminal, pulled_off
            )
            if candidate is None:
                reason = gate or RejectReason.MISSING_WEIGHT
                gate_rejections.append((bag_id, reason))
                self._commit_gate_rejection(flight_id, batch, bag_id, reason)
                continue
            candidates.append(candidate)
            counted.add(bag_id)

        planned = plan_batch(
            flight_id, batch, candidates, constraints, policy=self._policy
        )
        return self._commit_plan(planned, envelope, constraints, gate_rejections)

    # -- 落账 --------------------------------------------------------------

    def _commit_plan(
        self,
        plan: PlannedBatch,
        envelope: EnvelopeConfig,
        constraints: LoadConstraints,
        gate_rejections: list[tuple[str, RejectReason]] | None = None,
    ) -> BatchPlanResult:
        gate_rejections = gate_rejections or []
        at = self._clock.now_iso()
        # 先重放策略步骤得到最终装载顺序，全前缀复核通过才构造记录、进会话。
        final_order = self._replay_order(plan.steps)
        if not prefixes_hold(final_order, constraints):
            raise OrderingError("load plan failed post-rollback revalidation")

        # 换装是原子结论组：ROLLED_BACK 仅注解撤板、不动载重账，账态迁移只发生
        # 在 PULLED_OFF（减）与 SWAPPED_IN（加）。注解行携带组提交后的最终合计。
        records: list[tuple[LoadDecision, Step]] = []
        running: list[PlacedPiece] = []
        group: list[Step] = []

        def balance_of(pieces: list[PlacedPiece]):
            return total(pieces, constraints)

        def issue(steps: list[Step], settled: list[PlacedPiece], seq_start: int) -> int:
            seq = seq_start
            settled_balance = balance_of(settled)
            for queued in steps:
                decision = LoadDecision(
                    seq=seq,
                    flight_id=plan.flight_id,
                    batch=plan.batch,
                    bag_id=queued.bag_id,
                    kind=queued.kind,
                    reason=queued.reason,
                    weight_g=queued.weight_g,
                    arm_cm=queued.arm_cm,
                    position=queued.position
                    if queued.kind in (DecisionKind.CONFIRMED, DecisionKind.SWAPPED_IN)
                    else -1,
                    policy=plan.policy.value,
                    detail=dict(queued.detail),
                    swapped_for=queued.swapped_for,
                    balance_weight_g=settled_balance.weight_g,
                    balance_cg_cm=settled_balance.cg_cm,
                    at=at,
                )
                records.append((decision, queued))
                seq += 1
            return seq

        next_seq = self._store.state().load_seq.get(plan.flight_id, 0) + 1
        for step in plan.steps:
            piece = PlacedPiece(step.bag_id, step.weight_g, step.arm_cm)
            if step.kind is DecisionKind.CONFIRMED:
                running.append(piece)
                next_seq = issue([step], running, next_seq)
            elif step.kind is DecisionKind.REJECTED:
                next_seq = issue([step], running, next_seq)
            elif step.kind is DecisionKind.ROLLED_BACK:
                group.append(step)
            elif step.kind is DecisionKind.PULLED_OFF:
                group.append(step)
                running = [item for item in running if item.bag_id != step.bag_id]
            elif step.kind is DecisionKind.SWAPPED_IN:
                group.append(step)
                running.append(piece)
                # 组内的撤板注解行共用换装落定后的合计。
                next_seq = issue(group, running, next_seq)
                group = []
        if group:  # 防御：PULLED_OFF 后必有 SWAPPED_IN，不应残留
            next_seq = issue(group, running, next_seq)

        with self._store.session() as session:
            for decision, _step in records:
                self._ledger.append_decision(session, decision, at)

        return self._result_from_records(
            plan.flight_id, plan.batch, plan.policy, records, envelope, constraints,
            gate_rejections,
        )

    @staticmethod
    def _replay_order(steps: list[Step]) -> list[PlacedPiece]:
        """按策略步骤重放最终装载顺序（仅 CONFIRMED/SWAPPED_IN 占位）。"""

        order: list[PlacedPiece] = []
        for step in steps:
            piece = PlacedPiece(step.bag_id, step.weight_g, step.arm_cm)
            if step.kind is DecisionKind.CONFIRMED:
                order.append(piece)
            elif step.kind is DecisionKind.SWAPPED_IN:
                order.append(piece)
            elif step.kind is DecisionKind.PULLED_OFF:
                order = [item for item in order if item.bag_id != step.bag_id]
        return order

    def _commit_gate_rejection(
        self, flight_id: str, batch: int, bag_id: str, reason: RejectReason
    ) -> LoadDecision:
        at = self._clock.now_iso()
        record = self._store.state().weighings.get(bag_id)
        weight = record.weight_g if record is not None and record.weight_g > 0 else 0
        arm = record.arm_cm if record is not None else 0
        with self._store.session() as session:
            seq = session.state.load_seq.get(flight_id, 0) + 1
            decision = LoadDecision(
                seq=seq,
                flight_id=flight_id,
                batch=batch,
                bag_id=bag_id,
                kind=DecisionKind.REJECTED,
                reason=reason,
                weight_g=weight,
                arm_cm=arm,
                position=-1,
                policy=self._policy.value,
                detail={"gate": reason.value},
                balance_weight_g=self._safe_weight(session.state, flight_id),
                balance_cg_cm=self._safe_cg(session.state, flight_id),
                at=at,
            )
            self._ledger.append_decision(session, decision, at)
        return decision

    # -- 读模型 ------------------------------------------------------------

    def loading_order(self, flight_id: str) -> list[str]:
        """仍在载重账上的确认件，按装载位序排列。

        重放规则：CONFIRMED 追加；SWAPPED_IN 是换装触发件，入队尾；
        PULLED_OFF 把槽位重件移出；ROLLED_BACK 仅注解撤板，不改变账面顺序。
        """

        order: list[str] = []
        for decision in self._ledger.decisions(flight_id):
            if decision.kind is DecisionKind.CONFIRMED:
                order.append(decision.bag_id)
            elif decision.kind is DecisionKind.SWAPPED_IN:
                order.append(decision.bag_id)
            elif decision.kind is DecisionKind.PULLED_OFF:
                if decision.bag_id in order:
                    order.remove(decision.bag_id)
        return order

    def account(self, flight_id: str) -> dict[str, Any]:
        """载重额度账：逐笔流水 + 合计，便于逐笔核对。"""

        envelope = self._ledger.envelope(flight_id)
        if envelope is None:
            raise NotFoundError(f"flight {flight_id!r} has no weight envelope")
        rows: list[dict[str, Any]] = []
        running_weight = envelope.base_weight_g
        running_moment = envelope.base_moment_gcm
        for decision in self._ledger.decisions(flight_id):
            delta = 0
            if decision.kind in (DecisionKind.CONFIRMED, DecisionKind.SWAPPED_IN):
                delta = decision.weight_g
                running_weight += decision.weight_g
                running_moment += decision.weight_g * decision.arm_cm
            elif decision.kind is DecisionKind.PULLED_OFF:
                delta = -decision.weight_g
                running_weight -= decision.weight_g
                running_moment -= decision.weight_g * decision.arm_cm
            else:
                # ROLLED_BACK 是换装组内的撤板注解：resume 件仍在舱单、victim
                # 的减重由同组 PULLED_OFF 行承载，故注解行不改变载重账。
                delta = 0
            cg = running_moment / running_weight if running_weight else 0.0
            rows.append(
                {
                    "seq": decision.seq,
                    "bag_id": decision.bag_id,
                    "kind": decision.kind.value,
                    "reason": decision.reason.value if decision.reason else "",
                    "delta_g": delta,
                    "balance_g": running_weight,
                    "balance_cg_cm": round(cg, 3),
                    "at": decision.at,
                }
            )
        return {
            "flight_id": flight_id,
            "envelope_version": envelope.version,
            "deadload_cap_g": envelope.deadload_cap_g,
            "headroom_g": envelope.deadload_cap_g - running_weight,
            "cg_window": [envelope.cg_min_cm, envelope.cg_max_cm],
            "balance_g": running_weight,
            "balance_cg_cm": round(
                running_moment / running_weight if running_weight else 0.0, 3
            ),
            "entries": rows,
        }

    def decisions_view(self, flight_id: str | None = None) -> list[dict[str, Any]]:
        return [decision.as_dict() for decision in self._ledger.decisions(flight_id)]

    # -- 内部工具 ----------------------------------------------------------

    @staticmethod
    def _to_constraints(envelope: EnvelopeConfig) -> LoadConstraints:
        return LoadConstraints(
            deadload_cap_g=envelope.deadload_cap_g,
            cg_min_cm=envelope.cg_min_cm,
            cg_max_cm=envelope.cg_max_cm,
            base_weight_g=envelope.base_weight_g,
            base_moment_gcm=envelope.base_moment_gcm,
            piece_max_g=envelope.piece_max_g,
        )

    def _safe_weight(self, state, flight_id: str) -> int:
        """拒绝落账时的账面重量；包线缺失（如未配置即拒）时记 0。"""

        envelope = state.envelopes.get(flight_id)
        weight = envelope.base_weight_g if envelope else 0
        for bag_id in self._counted_from(state):
            record = state.weighings.get(bag_id)
            if record is not None:
                weight += record.weight_g
        return weight

    def _safe_cg(self, state, flight_id: str) -> float:
        envelope = state.envelopes.get(flight_id)
        moment = envelope.base_moment_gcm if envelope else 0
        weight = envelope.base_weight_g if envelope else 0
        for bag_id in self._counted_from(state):
            record = state.weighings.get(bag_id)
            if record is not None:
                moment += record.weight_g * record.arm_cm
                weight += record.weight_g
        return moment / weight if weight else 0.0

    @staticmethod
    def _counted_from(state) -> set[str]:
        counted: set[str] = set()
        for decision in state.load_decisions:
            if decision.kind in (DecisionKind.CONFIRMED, DecisionKind.SWAPPED_IN):
                counted.add(decision.bag_id)
            elif decision.kind in (DecisionKind.ROLLED_BACK, DecisionKind.PULLED_OFF):
                counted.discard(decision.bag_id)
        return counted

    @staticmethod
    def _reject_decision(
        bag_id: str, reason: RejectReason, weight_g: int, detail: dict[str, Any]
    ) -> PlanDecision:
        return PlanDecision(
            bag_id=bag_id,
            accepted=False,
            kind=DecisionKind.REJECTED.value,
            reason=reason.value,
            reason_text=REASON_TEXT[reason],
            weight_g=weight_g,
            position=-1,
            detail=detail,
        )

    def _result_from_records(
        self,
        flight_id: str,
        batch: int,
        policy: OverlimitPolicy,
        records: list[tuple[LoadDecision, Step]],
        envelope: EnvelopeConfig,
        constraints: LoadConstraints,
        gate_rejections: list[tuple[str, RejectReason]],
    ) -> BatchPlanResult:
        decisions: list[PlanDecision] = []
        for _decision, step in records:
            if step.kind in (DecisionKind.CONFIRMED, DecisionKind.SWAPPED_IN):
                decisions.append(
                    PlanDecision(
                        bag_id=step.bag_id,
                        accepted=True,
                        kind=step.kind.value,
                        reason="",
                        reason_text="",
                        weight_g=step.weight_g,
                        position=step.position,
                        detail=dict(step.detail),
                    )
                )
            elif step.kind is DecisionKind.REJECTED:
                reason = step.reason or RejectReason.NO_FEASIBLE_SWAP
                decisions.append(
                    self._reject_decision(step.bag_id, reason, step.weight_g, dict(step.detail))
                )
            elif step.kind is DecisionKind.PULLED_OFF:
                reason = step.reason or RejectReason.PULLED_OFF_FOR_SWAP
                decisions.append(
                    PlanDecision(
                        bag_id=step.bag_id,
                        accepted=False,
                        kind=step.kind.value,
                        reason=reason.value,
                        reason_text=REASON_TEXT[reason],
                        weight_g=step.weight_g,
                        position=-1,
                        detail=dict(step.detail),
                    )
                )
            else:  # ROLLED_BACK
                decisions.append(
                    PlanDecision(
                        bag_id=step.bag_id,
                        accepted=False,
                        kind=step.kind.value,
                        reason="",
                        reason_text="已确认装载顺序回滚",
                        weight_g=step.weight_g,
                        position=-1,
                        detail=dict(step.detail),
                    )
                )
        for bag_id, reason in gate_rejections:
            decisions.append(self._reject_decision(bag_id, reason, 0, {"gate": reason.value}))
        order = self.loading_order(flight_id)
        pieces = [
            PlacedPiece(bag_id, self._ledger.weighing(bag_id).weight_g,
                        self._ledger.weighing(bag_id).arm_cm)
            for bag_id in order
        ]
        outcome = evaluate(pieces, constraints)
        rejected = [
            decision.bag_id
            for decision in decisions
            if decision.kind == DecisionKind.REJECTED.value
        ]
        pulled_off = [
            decision.bag_id
            for decision in decisions
            if decision.kind == DecisionKind.PULLED_OFF.value
        ]
        return BatchPlanResult(
            flight_id=flight_id,
            batch=batch,
            policy=policy.value,
            decisions=decisions,
            order=order,
            weight_g=outcome.weight_g,
            headroom_g=outcome.headroom_g,
            cg_cm=outcome.cg_cm,
            cg_window=(envelope.cg_min_cm, envelope.cg_max_cm),
            rejected=rejected,
            pulled_off=pulled_off,
        )

    def _empty_result(
        self,
        flight_id: str,
        batch: int,
        bag_ids: list[str],
        reason: RejectReason,
    ) -> BatchPlanResult:
        envelope = self._ledger.envelope(flight_id)
        decisions = [
            self._reject_decision(bag_id, reason, 0, {"gate": reason.value})
            for bag_id in bag_ids
        ]
        return BatchPlanResult(
            flight_id=flight_id,
            batch=batch,
            policy=self._policy.value,
            decisions=decisions,
            order=[],
            weight_g=envelope.base_weight_g if envelope else 0,
            headroom_g=envelope.deadload_cap_g - envelope.base_weight_g if envelope else 0,
            cg_cm=envelope.base_moment_gcm / envelope.base_weight_g
            if envelope and envelope.base_weight_g
            else 0.0,
            cg_window=(
                (envelope.cg_min_cm, envelope.cg_max_cm) if envelope else (0.0, 0.0)
            ),
            rejected=list(bag_ids),
            pulled_off=[],
        )
