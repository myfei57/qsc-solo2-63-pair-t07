"""配载子系统使用的枚举。"""

from __future__ import annotations

from enum import Enum


class OverlimitPolicy(str, Enum):
    """候选件试装越限时的处置策略。"""

    REJECT_LAST = "reject_last"
    SWAP_LIGHTER = "swap_lighter"


class DecisionKind(str, Enum):
    """逐笔配载结论。"""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SWAPPED_IN = "swapped_in"
    PULLED_OFF = "pulled_off"
    ROLLED_BACK = "rolled_back"


class RejectReason(str, Enum):
    """拒绝或换装结论的原因码，导出件与审计留痕原样携带。"""

    MISSING_WEIGHT = "missing_weight"
    INVALID_WEIGHT = "invalid_weight"
    PIECE_OVERWEIGHT = "piece_overweight"
    DEADLOAD_EXCEEDED = "deadload_exceeded"
    CG_OUT_OF_WINDOW = "cg_out_of_window"
    ALREADY_COUNTED = "already_counted"
    FLIGHT_NOT_PLANNABLE = "flight_not_plannable"
    NO_FEASIBLE_SWAP = "no_feasible_swap"
    PULLED_OFF_FOR_SWAP = "pulled_off_for_swap"
    PULLED_OFF_RECOUNT = "pulled_off_recount"

    @property
    def terminal(self) -> bool:
        """终态原因：本航班不得再把该件计入重量。

        ``MISSING_WEIGHT`` 与 ``INVALID_WEIGHT`` 不是终态：补过秤、复核秤量后
        可重新提交。被换装撤下的件（``PULLED_OFF_FOR_SWAP``）同样不是终态，
        它是合法行李，只是本批次被移出，可交人工改配后续航班/批次。
        """

        return self in (
            RejectReason.PIECE_OVERWEIGHT,
            RejectReason.DEADLOAD_EXCEEDED,
            RejectReason.CG_OUT_OF_WINDOW,
            RejectReason.ALREADY_COUNTED,
            RejectReason.FLIGHT_NOT_PLANNABLE,
            RejectReason.NO_FEASIBLE_SWAP,
            RejectReason.PULLED_OFF_RECOUNT,
        )
