"""把配载结论转成与全链路一致的哈希链接审计条目。

复用 :func:`bagsort.audit.chain.digest_of`，使配载留痕与既有审计链同链同算法，
``/api/audit/verify`` 能一并校验。阶段取既有拓扑中的 ``belt``（上转盘装载）。
"""

from __future__ import annotations

from bagsort.audit.chain import digest_of
from bagsort.model.entities import AuditEntry

from .entities import LoadDecision
from .enums import DecisionKind

_KIND_MAP = {
    DecisionKind.CONFIRMED: "loadplan_confirmed",
    DecisionKind.REJECTED: "loadplan_rejected",
    DecisionKind.SWAPPED_IN: "loadplan_swapped_in",
    DecisionKind.PULLED_OFF: "loadplan_pulled_off",
    DecisionKind.ROLLED_BACK: "loadplan_rolled_back",
}


def audit_kind(kind: DecisionKind) -> str:
    return _KIND_MAP[kind]


def build_audit_entry(
    count: int,
    previous: str,
    decision: LoadDecision,
    at: str,
) -> AuditEntry:
    """构造一条链接到前序摘要的配载审计条目（不落盘）。"""

    kind = audit_kind(decision.kind)
    detail: dict[str, object] = {
        "stage": "belt",
        "stage_index": 6,
        "flight_id": decision.flight_id,
        "batch": decision.batch,
        "load_seq": decision.seq,
        "decision": decision.kind.value,
        "policy": decision.policy,
        "weight_g": decision.weight_g,
        "arm_cm": decision.arm_cm,
        "position": decision.position,
        "balance_weight_g": decision.balance_weight_g,
        "balance_cg_cm": round(decision.balance_cg_cm, 3),
    }
    if decision.reason is not None:
        detail["reason"] = decision.reason.value
    if decision.swapped_for:
        detail["swapped_for"] = decision.swapped_for
    if decision.detail:
        detail["context"] = decision.detail
    entry = AuditEntry(
        sequence=count,
        kind=kind,
        subject=decision.bag_id,
        detail=detail,
        prev_digest=previous,
        at=at,
    )
    return AuditEntry(
        sequence=entry.sequence,
        kind=entry.kind,
        subject=entry.subject,
        detail=entry.detail,
        prev_digest=entry.prev_digest,
        digest=digest_of(entry),
        at=entry.at,
    )
