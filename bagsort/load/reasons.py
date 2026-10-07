"""Machine-readable rejection codes and their Chinese explanations.

Every refusal the loadmaster produces carries both a stable ``code`` (so
downstream systems and exports can act on it) and a human-readable ``reason`` in
Chinese (so the operator at the scale or carousel understands immediately why a
piece was pulled).
"""

from __future__ import annotations

# A bag arrived for loading without an accepted weighing record.
MISSING_WEIGHING = "MISSING_WEIGHING"
# The scale produced a non-positive or otherwise unusable reading.
BAD_WEIGHING = "BAD_WEIGHING"
# The single piece alone exceeds the per-piece limit (overweight at intake).
PIECE_OVERWEIGHT = "PIECE_OVERWEIGHT"
# The same bag would be counted in a batch while already counted in another.
DUPLICATE_BATCH = "DUPLICATE_BATCH"
# The flight has no loadmaster configuration on record.
NO_LIMITS = "NO_LIMITS"
# The bag is not in a loadable state for this operation.
NOT_LOADABLE = "NOT_LOADABLE"
# Loading this piece anywhere would breach weight and/or balance bounds.
PLACEMENT_BREACH = "PLACEMENT_BREACH"
# The flight still has sorting allowance in pieces but none can be spent.
PIECE_QUOTA = "PIECE_QUOTA"
# A swap precondition failed before any confirmed order was touched.
SWAP_PRECONDITION = "SWAP_PRECONDITION"
# A swap could not be re-planned; the confirmed order was rolled back intact.
SWAP_INFEASIBLE = "SWAP_INFEASIBLE"
# A rollback target is not an ancestor of the current confirmed version.
ROLLBACK_STALE = "ROLLBACK_STALE"

REASONS: dict[str, str] = {
    MISSING_WEIGHING: "缺称重记录：该件未过秤或称重未通过，先扣留复秤，不得计入重量",
    BAD_WEIGHING: "称重数据异常：读数必须为正数，请复秤后再交",
    PIECE_OVERWEIGHT: "单件超重：该件重量超过单件上限，在转盘前截留",
    DUPLICATE_BATCH: "重复计入：该件已在本航班已确认装载序列中，不能在另一批次重复计重",
    NO_LIMITS: "未配置载重/重心限额，配载未放行",
    NOT_LOADABLE: "当前状态不可装载：只有已称重且未装机的件可以进入序列",
    PLACEMENT_BREACH: "载重或重心越限：该件装入任何剩余舱位都会越界，按越限拒装最后一件处理",
    PIECE_QUOTA: "件数额度已用尽：该航班剩余件数额度为零",
    SWAP_PRECONDITION: "换装前置条件不满足，未改动已确认序列",
    SWAP_INFEASIBLE: "换装后无法同时满足载重上限与重心窗口，已回滚，原序列不变",
    ROLLBACK_STALE: "回滚目标不是当前序列的历史版本，拒绝回滚",
}


def explain(code: str, **parts: object) -> str:
    """Return the Chinese explanation, optionally refined with evidence."""

    return REASONS.get(code, code)
