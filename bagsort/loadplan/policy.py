"""越限处置策略：拒最后一件 / 较轻件换装（含已确认顺序回滚）。

本模块是纯逻辑：输入候选件与约束，输出一个逐件结论计划，不碰存储与时钟。
:mod:`bagsort.loadplan.planner` 负责把计划逐笔落账。

两种策略的代价（也写入 docs/载重配载-决策.md）：

* **拒最后一件（reject_last）**：只动尚未确认的候选，已确认装载的旅客不受
  影响，装机人员无返工；代价是触发越限的那位旅客在登机口才被告知行李拉下，
  行程确定性差，且货舱里已有的偏重排布无法被后来的轻件改善，舱位利用率低。
* **较轻件换装（swap_lighter，本系统默认）**：从已确认顺序里回滚一件更重
  的、用较轻件顶替，再恢复其后已确认件，多保一件上机，舱位利用更高、被拒
  旅客可更早分流；代价是已向旅客确认过的装载要被撤回，装机端有一次撤板/
  重装返工，且回滚后必须对整段顺序重新验证——这正是 :func:`plan_batch`
  每步都做的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import DecisionKind, OverlimitPolicy, RejectReason
from .physics import LoadConstraints, PlacedPiece, prefixes_hold, trial

REJECT_LAST = OverlimitPolicy.REJECT_LAST
SWAP_LIGHTER = OverlimitPolicy.SWAP_LIGHTER


@dataclass(frozen=True)
class Candidate:
    """提交配载的一件行李（必须已过秤；缺秤件不进策略层）。"""

    bag_id: str
    weight_g: int
    arm_cm: int = 0

    def as_piece(self) -> PlacedPiece:
        return PlacedPiece(self.bag_id, self.weight_g, self.arm_cm)


@dataclass
class Step:
    """策略对一件候选给出的一步结论。"""

    bag_id: str
    kind: DecisionKind
    reason: RejectReason | None = None
    weight_g: int = 0
    arm_cm: int = 0
    position: int = -1
    swapped_for: str = ""
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class PlannedBatch:
    """一个批次的完整计划：逐笔步骤 + 最终确认顺序。"""

    flight_id: str
    batch: int
    policy: OverlimitPolicy
    steps: list[Step] = field(default_factory=list)
    order: list[PlacedPiece] = field(default_factory=list)

    def confirmed(self) -> list[Step]:
        return [
            step
            for step in self.steps
            if step.kind in (DecisionKind.CONFIRMED, DecisionKind.SWAPPED_IN)
        ]

    def rejected(self) -> list[Step]:
        return [step for step in self.steps if step.kind is DecisionKind.REJECTED]


def _find_swap_slot(
    order: list[PlacedPiece],
    trigger: Candidate,
    constraints: LoadConstraints,
) -> int | None:
    """找换装槽：撤下的确认件必须更重，换装后的整段新顺序必须逐前缀可行。

    换装后的装载顺序为「去掉槽位件，触发件排到队尾」（触发件最晚到，自然最后
    装）。逐前缀都要同时满足载重上限与重心窗口，而不只是最终状态可行。

    选满足条件的最轻件（同重取位序最靠后），把撤件影响压到最小。
    """

    feasible_slots: list[tuple[int, int]] = []
    for position, piece in enumerate(order):
        if piece.weight_g <= trigger.weight_g:
            continue
        revised = order[:position] + order[position + 1 :] + [trigger.as_piece()]
        if prefixes_hold(revised, constraints):
            feasible_slots.append((piece.weight_g, -position))
    if not feasible_slots:
        return None
    _weight, neg_position = min(feasible_slots)
    return -neg_position


def _emit_swap(
    plan: "PlannedBatch",
    trigger: Candidate,
    position: int,
) -> Step:
    """把一次换装展开成可逐笔留痕、可重放的原子步骤组。

    物理顺序：为够到槽位上的重件，槽位及其后的已确认件按后入先出逐件撤板
    （ROLLED_BACK 审计注解）；其中槽位重件被正式撤下交人工改配（PULLED_OFF，
    载重账减重量），其后的件撤板后原位恢复（``resume=True``，仍计入载重）；
    最后触发件以较轻件身份入列（SWAPPED_IN，记在队尾）。整组在同一个存储
    会话提交，要么组内全部可见，要么整组不见。
    """

    rolled = plan.order[position:]
    victim = plan.order[position]
    for piece in reversed(rolled):
        plan.steps.append(
            Step(
                bag_id=piece.bag_id,
                kind=DecisionKind.ROLLED_BACK,
                weight_g=piece.weight_g,
                arm_cm=piece.arm_cm,
                detail={
                    "rolled_back_for": trigger.bag_id,
                    "resume": piece.bag_id != victim.bag_id,
                },
            )
        )
    plan.steps.append(
        Step(
            bag_id=victim.bag_id,
            kind=DecisionKind.PULLED_OFF,
            reason=RejectReason.PULLED_OFF_FOR_SWAP,
            weight_g=victim.weight_g,
            arm_cm=victim.arm_cm,
            swapped_for=trigger.bag_id,
            detail={"former_position": position},
        )
    )
    plan.order = plan.order[:position] + plan.order[position + 1 :] + [trigger.as_piece()]
    swapped_in = Step(
        bag_id=trigger.bag_id,
        kind=DecisionKind.SWAPPED_IN,
        weight_g=trigger.weight_g,
        arm_cm=trigger.arm_cm,
        position=len(plan.order) - 1,
        swapped_for=victim.bag_id,
        detail={
            "rolled_back_depth": len(rolled),
            "pulled_off_weight_g": victim.weight_g,
            "gained_headroom_g": victim.weight_g - trigger.weight_g,
        },
    )
    plan.steps.append(swapped_in)
    return swapped_in


def _reject(trigger: Candidate, result_reason: str, no_swap: bool = False) -> Step:
    if no_swap:
        # 主因仍是越限（载重或重心）；无可行换装只决定"怎么处置"，故原因码用
        # no_feasible_swap，而把原始越限类型放进 detail.violation 供导出与审计区分。
        reason = RejectReason.NO_FEASIBLE_SWAP
    else:
        mapping = {
            "piece_overweight": RejectReason.PIECE_OVERWEIGHT,
            "deadload_exceeded": RejectReason.DEADLOAD_EXCEEDED,
            "cg_out_of_window": RejectReason.CG_OUT_OF_WINDOW,
        }
        reason = mapping.get(result_reason, RejectReason.NO_FEASIBLE_SWAP)
    return Step(
        bag_id=trigger.bag_id,
        kind=DecisionKind.REJECTED,
        reason=reason,
        weight_g=trigger.weight_g,
        arm_cm=trigger.arm_cm,
        detail={
            "violation": result_reason,
            "swap_attempted": no_swap,
            "policy": "reject_last_fallback" if no_swap else "reject_last",
        },
    )


def plan_batch(
    flight_id: str,
    batch: int,
    candidates: list[Candidate],
    constraints: LoadConstraints,
    policy: OverlimitPolicy = SWAP_LIGHTER,
) -> PlannedBatch:
    """按提交顺序逐件试装，产出逐笔步骤与最终确认顺序。

    不变量：返回的 ``plan.order`` 的每个前缀都通过 :func:`prefixes_hold`，
    即换装回滚之后载重上限与重心窗口仍然同时成立。
    """

    plan = PlannedBatch(flight_id=flight_id, batch=batch, policy=policy)
    for candidate in candidates:
        result = trial(plan.order, candidate.as_piece(), constraints)
        if result.feasible:
            position = len(plan.order)
            plan.order.append(candidate.as_piece())
            plan.steps.append(
                Step(
                    bag_id=candidate.bag_id,
                    kind=DecisionKind.CONFIRMED,
                    weight_g=candidate.weight_g,
                    arm_cm=candidate.arm_cm,
                    position=position,
                    detail={
                        "headroom_g": result.headroom_g,
                        "cg_cm": round(result.cg_cm, 3),
                    },
                )
            )
            continue
        # 单件超规无药可救；其余越限在换装策略下先找可行换装槽。
        slot: int | None = None
        if policy is SWAP_LIGHTER and result.reason != "piece_overweight":
            slot = _find_swap_slot(plan.order, candidate, constraints)
        if slot is not None:
            _emit_swap(plan, candidate, slot)
        else:
            attempted_swap = (
                policy is SWAP_LIGHTER and result.reason != "piece_overweight"
            )
            plan.steps.append(_reject(candidate, result.reason, attempted_swap))
    assert prefixes_hold(plan.order, constraints), "planned prefixes must all be feasible"
    return plan
