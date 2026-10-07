"""CSV rendering used by the export endpoints."""

from __future__ import annotations

import csv
import io
from typing import Iterable, Sequence


def render(columns: Sequence[str], rows: Iterable[dict[str, object]]) -> str:
    """Render a header plus one line per row, in the column order given."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue()


def audit_rows(entries: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "sequence": entry.sequence,
            "kind": entry.kind,
            "subject": entry.subject,
            "stage": entry.detail.get("stage", ""),
            "at": entry.at,
            "digest": entry.digest,
        }
        for entry in entries
    ]


def sort_rows(records: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "bag_id": record.bag_id,
            "flight_id": record.flight_id,
            "chute_id": record.chute_id,
            "sequence": record.sequence,
            "epoch": record.epoch,
            "at": record.at,
        }
        for record in records
    ]


def load_rows(records: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "bag_id": record.bag_id,
            "carousel": record.carousel,
            "batch": record.batch,
            "at": record.at,
        }
        for record in records
    ]


def weighing_rows(records: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "bag_id": record.bag_id,
            "weight_g": record.weight_g,
            "arm_cm": record.arm_cm,
            "weighing_seq": record.weighing_seq,
            "at": record.at,
        }
        for record in records
    ]


def loadplan_rows(decisions: Iterable[object]) -> list[dict[str, object]]:
    """逐笔配载结论导出：越限、拒绝、换装、回滚逐笔可核。"""

    return [
        {
            "seq": decision.seq,
            "flight_id": decision.flight_id,
            "batch": decision.batch,
            "bag_id": decision.bag_id,
            "kind": decision.kind.value,
            "reason": decision.reason.value if decision.reason else "",
            "weight_g": decision.weight_g,
            "arm_cm": decision.arm_cm,
            "position": decision.position,
            "policy": decision.policy,
            "swapped_for": decision.swapped_for,
            "balance_weight_g": decision.balance_weight_g,
            "balance_cg_cm": round(decision.balance_cg_cm, 3),
            "at": decision.at,
        }
        for decision in decisions
    ]


AUDIT_COLUMNS = ("sequence", "kind", "subject", "stage", "at", "digest")
SORT_COLUMNS = ("bag_id", "flight_id", "chute_id", "sequence", "epoch", "at")
LOAD_COLUMNS = ("bag_id", "carousel", "batch", "at")
WEIGHING_COLUMNS = ("bag_id", "weight_g", "arm_cm", "weighing_seq", "at")
LOADPLAN_COLUMNS = (
    "seq",
    "flight_id",
    "batch",
    "bag_id",
    "kind",
    "reason",
    "weight_g",
    "arm_cm",
    "position",
    "policy",
    "swapped_for",
    "balance_weight_g",
    "balance_cg_cm",
    "at",
)
