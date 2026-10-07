"""载重与重心计算：无 IO 的纯函数层。

航段配载的经典模型：每件货邮行（含基础业载）有重量 ``w`` 和站位力臂
``arm``（相对基准面的有符号距离）。总力矩 ``M = Σ w·arm``，合重心位置
``CG = M / Σw``。业载（deadload）不得超过货舱限额，合重心必须落在
``[cg_min, cg_max]`` 窗口内，且每个装载前缀都要同时满足两条约束——这样
系统给出的"先装哪些"顺序才是真正可以照着执行的顺序。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlacedPiece:
    """一件以指定站位计入载重的行李。"""

    bag_id: str
    weight_g: int
    arm_cm: int

    def __post_init__(self) -> None:
        if self.weight_g <= 0:
            raise ValueError("weight_g must be positive")


@dataclass(frozen=True)
class LoadConstraints:
    """一架次航段的载重上限与重心窗口。

    ``base_weight_g`` / ``base_moment_gcm`` 是不含行李的既有业载（旅客、
    货邮、燃油等）折算的重量与力矩，行李在其上累加。
    """

    deadload_cap_g: int
    cg_min_cm: float
    cg_max_cm: float
    base_weight_g: int = 0
    base_moment_gcm: int = 0
    piece_max_g: int = 0

    def __post_init__(self) -> None:
        if self.deadload_cap_g < 0:
            raise ValueError("deadload_cap_g must not be negative")
        if self.base_weight_g < 0:
            raise ValueError("base_weight_g must not be negative")
        if self.cg_min_cm > self.cg_max_cm:
            raise ValueError("cg_min_cm must not exceed cg_max_cm")
        if self.piece_max_g < 0:
            raise ValueError("piece_max_g must not be negative")

    @property
    def has_piece_limit(self) -> bool:
        return self.piece_max_g > 0


@dataclass(frozen=True)
class WeightBalance:
    """一个装载状态的合计值。"""

    weight_g: int
    moment_gcm: int

    @property
    def cg_cm(self) -> float:
        return self.moment_gcm / self.weight_g if self.weight_g else 0.0


@dataclass(frozen=True)
class TrialResult:
    """试装结论：越限时同时给出是哪一条约束、越了多少。"""

    feasible: bool
    reason: str = ""
    weight_g: int = 0
    moment_gcm: int = 0
    cg_cm: float = 0.0
    headroom_g: int = 0
    cg_slack_low: float = 0.0
    cg_slack_high: float = 0.0

    @property
    def balance(self) -> WeightBalance:
        return WeightBalance(self.weight_g, self.moment_gcm)


def total(pieces: list[PlacedPiece], constraints: LoadConstraints) -> WeightBalance:
    """基础业载 + 给定装载的合计重量与力矩。"""

    weight = constraints.base_weight_g
    moment = constraints.base_moment_gcm
    for piece in pieces:
        weight += piece.weight_g
        moment += piece.weight_g * piece.arm_cm
    return WeightBalance(weight, moment)


def evaluate(pieces: list[PlacedPiece], constraints: LoadConstraints) -> TrialResult:
    """判定一个装载集合（或前缀）是否同时满足载重与重心约束。"""

    balance = total(pieces, constraints)
    headroom = constraints.deadload_cap_g - balance.weight_g
    cg = balance.cg_cm
    slack_low = cg - constraints.cg_min_cm
    slack_high = constraints.cg_max_cm - cg
    if headroom < 0:
        reason = "deadload_exceeded"
        feasible = False
    elif slack_low < -1e-9 or slack_high < -1e-9:
        reason = "cg_out_of_window"
        feasible = False
    else:
        reason = ""
        feasible = True
    return TrialResult(
        feasible=feasible,
        reason=reason,
        weight_g=balance.weight_g,
        moment_gcm=balance.moment_gcm,
        cg_cm=cg,
        headroom_g=headroom,
        cg_slack_low=slack_low,
        cg_slack_high=slack_high,
    )


def trial(
    pieces: list[PlacedPiece],
    candidate: PlacedPiece,
    constraints: LoadConstraints,
) -> TrialResult:
    """把 ``candidate`` 叠到既有前缀 ``pieces`` 上试装。

    单件超限先于合计判定：再轻的排布也救不了一件本身超规的行李。
    """

    if constraints.has_piece_limit and candidate.weight_g > constraints.piece_max_g:
        balance = total(pieces + [candidate], constraints)
        return TrialResult(
            feasible=False,
            reason="piece_overweight",
            weight_g=balance.weight_g,
            moment_gcm=balance.moment_gcm,
            cg_cm=balance.cg_cm,
            headroom_g=constraints.deadload_cap_g - balance.weight_g,
            cg_slack_low=balance.cg_cm - constraints.cg_min_cm,
            cg_slack_high=constraints.cg_max_cm - balance.cg_cm,
        )
    return evaluate(pieces + [candidate], constraints)


def prefixes_hold(
    order: list[PlacedPiece],
    constraints: LoadConstraints,
) -> bool:
    """装载顺序的每个前缀都可行——"顺序可执行"的充要条件。"""

    prefix: list[PlacedPiece] = []
    for piece in order:
        result = trial(prefix, piece, constraints)
        if not result.feasible:
            return False
        prefix.append(piece)
    return True
