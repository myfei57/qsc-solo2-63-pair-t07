"""称重记录、额度账（包线）与配载结论的只增账本。

账本本身只负责"逐笔落日志 + 从物化状态读回"，不算重心、不做策略判定。
所有写入都是一个 store 会话里的命令；换装涉及的多笔结论（回滚、换上、
撤下）必须在同一会话提交，才能做到"要么整组成立、要么整组不见"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bagsort.clock import Clock
from bagsort.errors import ValidationError
from bagsort.model.entities import AuditEntry

from .audit import build_audit_entry
from .entities import EnvelopeConfig, LoadDecision, WeighingRecord
from .enums import DecisionKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bagsort.model.state import State
    from bagsort.store.store import Session as StoreSession
    from bagsort.store.store import Store

MIN_WEIGHT_G = 1


class LoadLedger:
    """称重、包线额度与装载结论三张表的读写门面。"""

    def __init__(self, store: "Store", clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- 称重记录 ----------------------------------------------------------

    def record_weighing(self, bag_id: str, weight_g: int, arm_cm: int) -> WeighingRecord:
        if not bag_id:
            raise ValidationError("bag id must not be empty")
        if weight_g < MIN_WEIGHT_G:
            raise ValidationError(f"weight_g must be >= {MIN_WEIGHT_G}")
        at = self._clock.now_iso()
        with self._store.session() as session:
            seq = session.state.weighing_counter + 1
            record = WeighingRecord(
                bag_id=bag_id, weight_g=weight_g, arm_cm=arm_cm, at=at, weighing_seq=seq
            )
            session.state.weighings[bag_id] = record
            session.state.weighing_counter = seq
            session.record("weighing.put", {"record": record.as_dict()}, at)
        return record

    def weighing(self, bag_id: str) -> WeighingRecord | None:
        return self._store.state().weighings.get(bag_id)

    def weighings(self) -> list[WeighingRecord]:
        records = self._store.state().weighings
        return [records[key] for key in sorted(records)]

    # -- 额度账（包线） ----------------------------------------------------

    def set_envelope(
        self,
        flight_id: str,
        deadload_cap_g: int,
        cg_min_cm: float,
        cg_max_cm: float,
        base_weight_g: int = 0,
        base_moment_gcm: int = 0,
        piece_max_g: int = 0,
    ) -> EnvelopeConfig:
        if not flight_id:
            raise ValidationError("flight id must not be empty")
        if deadload_cap_g < 0 or base_weight_g < 0 or piece_max_g < 0:
            raise ValidationError("weights must not be negative")
        if cg_min_cm > cg_max_cm:
            raise ValidationError("cg_min_cm must not exceed cg_max_cm")
        at = self._clock.now_iso()
        with self._store.session() as session:
            previous = session.state.envelopes.get(flight_id)
            version = (previous.version + 1) if previous else 1
            envelope = EnvelopeConfig(
                flight_id=flight_id,
                deadload_cap_g=deadload_cap_g,
                cg_min_cm=cg_min_cm,
                cg_max_cm=cg_max_cm,
                base_weight_g=base_weight_g,
                base_moment_gcm=base_moment_gcm,
                piece_max_g=piece_max_g,
                version=version,
                at=at,
            )
            session.state.envelopes[flight_id] = envelope
            session.record("envelope.put", {"envelope": envelope.as_dict()}, at)
        return envelope

    def envelope(self, flight_id: str) -> EnvelopeConfig | None:
        return self._store.state().envelopes.get(flight_id)

    def envelopes(self) -> list[EnvelopeConfig]:
        envelopes = self._store.state().envelopes
        return [envelopes[key] for key in sorted(envelopes)]

    # -- 配载结论 ----------------------------------------------------------

    def next_seq(self, state: "State", flight_id: str) -> int:
        return state.load_seq.get(flight_id, 0) + 1

    def append_decision(self, session: "StoreSession", decision: LoadDecision, at: str) -> None:
        """在给定会话里追加一笔结论并同步写审计（原子提交的基本构件）。"""

        session.state.load_decisions.append(decision)
        session.state.load_seq[decision.flight_id] = max(
            session.state.load_seq.get(decision.flight_id, 0), decision.seq
        )
        session.record("load.decision", {"decision": decision.as_dict()}, at)
        entry = build_audit_entry(
            count=len(session.state.audit) + 1,
            previous=_last_digest(session.state.audit),
            decision=decision,
            at=at,
        )
        session.state.audit.append(entry)
        session.record("audit.append", {"entry": entry.as_dict()}, at)

    def decisions(self, flight_id: str | None = None) -> list[LoadDecision]:
        decisions = self._store.state().load_decisions
        if flight_id is None:
            return list(decisions)
        return [decision for decision in decisions if decision.flight_id == flight_id]

    def counted_bag_ids(self) -> set[str]:
        """当前仍计入某航班载重的行李集合（回滚/撤下后即释放）。"""

        counted: set[str] = set()
        for decision in self._store.state().load_decisions:
            if decision.kind in (DecisionKind.CONFIRMED, DecisionKind.SWAPPED_IN):
                counted.add(decision.bag_id)
            elif decision.kind in (DecisionKind.ROLLED_BACK, DecisionKind.PULLED_OFF):
                counted.discard(decision.bag_id)
        return counted

    def pulled_off_bag_ids(self) -> set[str]:
        """因换装被撤下的合法件：载重占用已释放，但须人工改配，系统不自动
        把它计回原航班（防止同一件被悄悄重复计入）。"""

        pulled: set[str] = set()
        for decision in self._store.state().load_decisions:
            if decision.kind is DecisionKind.PULLED_OFF:
                pulled.add(decision.bag_id)
            elif decision.kind in (DecisionKind.CONFIRMED, DecisionKind.SWAPPED_IN):
                pulled.discard(decision.bag_id)
        return pulled

    def terminal_bag_ids(self) -> set[str]:
        """已有终态结论（拒绝）的行李：本航班不得再计入重量。"""

        terminal: set[str] = set()
        for decision in self._store.state().load_decisions:
            if decision.kind is DecisionKind.REJECTED and decision.reason is not None:
                if decision.reason.terminal:
                    terminal.add(decision.bag_id)
        return terminal


def _last_digest(audit: list[AuditEntry]) -> str:
    from bagsort.audit.chain import GENESIS

    return audit[-1].digest if audit else GENESIS
