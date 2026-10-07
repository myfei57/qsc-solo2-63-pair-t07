"""载重与重心配载：上转盘前的装载计划子系统。

子系统分四层：

* :mod:`bagsort.loadplan.physics` 是纯函数：载重合计、合重心与重心窗口判定；
* :mod:`bagsort.loadplan.policy` 是越限策略：拒最后一件，或用较轻件换装（要回滚已确认件）；
* :mod:`bagsort.loadplan.ledger` 把称重记录、配载结论、载重账逐笔落进只增日志；
* :mod:`bagsort.loadplan.planner` 编排一个批次：逐件试装、给结论、记审计。

所有状态都经由既有的只增日志落盘，所以重启重放后载重账与装载顺序完全一致。
"""

from __future__ import annotations

from .enums import DecisionKind, OverlimitPolicy, RejectReason
from .ledger import LoadLedger
from .physics import LoadConstraints, PlacedPiece, TrialResult, trial
from .planner import LoadPlanner, PlanDecision
from .policy import REJECT_LAST, SWAP_LIGHTER, plan_batch

__all__ = [
    "DecisionKind",
    "LoadConstraints",
    "LoadLedger",
    "OverlimitPolicy",
    "PlanDecision",
    "PlacedPiece",
    "REJECT_LAST",
    "RejectReason",
    "SWAP_LIGHTER",
    "TrialResult",
    "LoadPlanner",
    "plan_batch",
    "trial",
]
