"""Export rendering for weight-and-balance records.

Every export is built from the same committed ledgers the console reads, and
every refusal row carries the ``audit_sequence`` / ``audit_digest`` anchor of
its audit entry: a refusal in a CSV that no longer matches the audit chain is a
detectable inconsistency, not an opinion. The bundle also embeds the
:meth:`LoadMaster.verify_flight` result, so "逐笔可核" travels with the file.
"""

from __future__ import annotations

from typing import Any

from bagsort.reporting.csv_export import render
from bagsort.store.store import Store
from .loadmaster import LoadMaster

WEIGH_COLUMNS = ("at", "bag_id", "flight_id", "weight", "scale_id", "accepted", "reason")
LEDGER_COLUMNS = ("at", "flight_id", "bag_id", "delta", "kind", "plan_version", "ref")
SEQUENCE_COLUMNS = (
    "version",
    "order",
    "bag_id",
    "weight",
    "position",
    "arm",
    "side",
)
REFUSAL_COLUMNS = (
    "at",
    "bag_id",
    "code",
    "reason",
    "audit_sequence",
    "audit_digest",
    "evidence",
)


def weigh_rows(store: Store, flight_id: str | None = None) -> list[dict[str, Any]]:
    records = store.state().weigh_records
    if flight_id is not None:
        records = [record for record in records if record.flight_id == flight_id]
    return [
        {
            "at": record.at,
            "bag_id": record.bag_id,
            "flight_id": record.flight_id,
            "weight": record.weight,
            "scale_id": record.scale_id,
            "accepted": "yes" if record.accepted else "no",
            "reason": record.reason,
        }
        for record in records
    ]


def ledger_rows(store: Store, flight_id: str | None = None) -> list[dict[str, Any]]:
    postings = store.state().weight_postings
    if flight_id is not None:
        postings = [posting for posting in postings if posting.flight_id == flight_id]
    return [
        {
            "at": posting.at,
            "flight_id": posting.flight_id,
            "bag_id": posting.bag_id,
            "delta": posting.delta,
            "kind": posting.kind,
            "plan_version": posting.plan_version,
            "ref": posting.ref,
        }
        for posting in postings
    ]


def sequence_rows(
    loadmaster: LoadMaster, flight_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    confirmed = loadmaster.confirmed(flight_id)
    version = confirmed.version if confirmed else 0
    rows = [
        {
            "version": version,
            "order": index,
            "bag_id": entry.bag_id,
            "weight": entry.weight,
            "position": entry.position,
            "arm": entry.arm,
            "side": entry.side,
        }
        for index, entry in enumerate(loadmaster.sequence(flight_id))
    ]
    refusals = [
        {
            "at": rejection.at,
            "bag_id": rejection.bag_id,
            "code": rejection.code,
            "reason": rejection.reason,
            "audit_sequence": rejection.audit_sequence,
            "audit_digest": rejection.audit_digest,
            "evidence": _compact_evidence(rejection.evidence),
        }
        for rejection in loadmaster.refusals(flight_id)
    ]
    return rows, refusals


def _compact_evidence(evidence: dict[str, Any]) -> str:
    import json

    return json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def export_bundle(loadmaster: LoadMaster, flight_id: str) -> dict[str, str]:
    """The four CSV exports plus a JSON manifest carrying verification results."""

    import json

    store = loadmaster.store
    sequence, refusals = sequence_rows(loadmaster, flight_id)
    verification = loadmaster.verify_flight(flight_id)
    report = loadmaster.loading_report(flight_id)
    manifest = {
        "flight_id": flight_id,
        "version": report["version"],
        "balance": report["balance"],
        "feasible": report["feasible"],
        "ledger_balance": report["ledger_balance"],
        "verification": verification,
        "files": ["weigh.csv", "ledger.csv", "sequence.csv", "refusals.csv"],
    }
    return {
        "weigh.csv": render(WEIGH_COLUMNS, weigh_rows(store, flight_id)),
        "ledger.csv": render(LEDGER_COLUMNS, ledger_rows(store, flight_id)),
        "sequence.csv": render(SEQUENCE_COLUMNS, sequence),
        "refusals.csv": render(REFUSAL_COLUMNS, refusals),
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
    }
