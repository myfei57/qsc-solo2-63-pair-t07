"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import BagState, FlightState


def _clean(raw: Any) -> Any:
    """Normalise nested containers so codecs round-trip deterministically."""

    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class Flight:
    id: str
    code: str
    terminal: str = "T1"
    carousel: str = ""
    chute: str = ""
    state: FlightState = FlightState.OPEN
    batch: int = 0
    schedule_generation: int = 1
    cutoff_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "terminal": self.terminal,
            "carousel": self.carousel,
            "chute": self.chute,
            "state": self.state.value,
            "batch": self.batch,
            "schedule_generation": self.schedule_generation,
            "cutoff_at": self.cutoff_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Flight":
        return cls(
            id=str(raw["id"]),
            code=str(raw.get("code", "")),
            terminal=str(raw.get("terminal", "T1")),
            carousel=str(raw.get("carousel", "")),
            chute=str(raw.get("chute", "")),
            state=FlightState(raw.get("state", FlightState.OPEN.value)),
            batch=int(raw.get("batch", 0)),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            closed_at=str(raw.get("closed_at", "")),
        )

    def evolved(self, **changes: Any) -> "Flight":
        return replace(self, **changes)


@dataclass(frozen=True)
class Bag:
    id: str
    barcode: str
    sequence: int
    epoch: int = 0
    flight_id: str = ""
    chute_id: str = ""
    state: BagState = BagState.CHECKED_IN

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Bag":
        return cls(
            id=str(raw["id"]),
            barcode=str(raw.get("barcode", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=BagState(raw.get("state", BagState.CHECKED_IN.value)),
        )

    def evolved(self, **changes: Any) -> "Bag":
        return replace(self, **changes)


@dataclass(frozen=True)
class Reading:
    barcode: str
    bag_id: str
    sequence: int
    epoch: int
    committed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "bag_id": self.bag_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reading":
        return cls(
            barcode=str(raw.get("barcode", "")),
            bag_id=str(raw.get("bag_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            committed=bool(raw.get("committed", False)),
        )


@dataclass(frozen=True)
class ChuteAssignment:
    flight_id: str
    chute_id: str
    generation: int = 1
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChuteAssignment":
        return cls(
            flight_id=str(raw["flight_id"]),
            chute_id=str(raw.get("chute_id", "")),
            generation=int(raw.get("generation", 1)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(frozen=True)
class SortRecord:
    bag_id: str
    flight_id: str
    chute_id: str
    sequence: int
    epoch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class TransferCommand:
    id: str
    flight_id: str
    carousel: str
    batch: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flight_id": self.flight_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferCommand":
        return cls(
            id=str(raw["id"]),
            flight_id=str(raw.get("flight_id", "")),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class LoadRecord:
    bag_id: str
    carousel: str
    batch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class WeightPosting:
    """One legible movement in a flight's weight ledger.

    The ledger is a signed, append-only account: loading posts ``+weight``
    (kind ``load``), removing a piece during a rollback or swap posts
    ``-weight`` (kind ``release``). ``bag_id`` plus ``plan_version`` make every
    posting traceable to the exact plan entry that authorised it, so the same
    piece can never be counted in two batches: the ledger and the plan can be
    cross-footed per bag.
    """

    flight_id: str
    bag_id: str
    delta: float
    kind: str
    plan_version: int
    at: str
    ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "bag_id": self.bag_id,
            "delta": self.delta,
            "kind": self.kind,
            "plan_version": self.plan_version,
            "at": self.at,
            "ref": self.ref,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WeightPosting":
        return cls(
            flight_id=str(raw["flight_id"]),
            bag_id=str(raw["bag_id"]),
            delta=float(raw.get("delta", 0.0)),
            kind=str(raw.get("kind", "")),
            plan_version=int(raw.get("plan_version", 0)),
            at=str(raw.get("at", "")),
            ref=str(raw.get("ref", "")),
        )


@dataclass(frozen=True)
class WeighRecord:
    """One weighing event. Readings are append-only; a re-weigh adds a row."""

    bag_id: str
    flight_id: str
    weight: float
    scale_id: str
    at: str
    accepted: bool = True
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "weight": self.weight,
            "scale_id": self.scale_id,
            "at": self.at,
            "accepted": self.accepted,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WeighRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            weight=float(raw.get("weight", 0.0)),
            scale_id=str(raw.get("scale_id", "")),
            at=str(raw.get("at", "")),
            accepted=bool(raw.get("accepted", True)),
            reason=str(raw.get("reason", "")),
        )


@dataclass(frozen=True)
class Rejection:
    """A refusal decision with machine-readable code, Chinese reason and evidence."""

    bag_id: str
    code: str
    reason: str
    at: str
    evidence: dict[str, Any] = field(default_factory=dict)
    audit_sequence: int = 0
    audit_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "code": self.code,
            "reason": self.reason,
            "at": self.at,
            "evidence": _clean(self.evidence),
            "audit_sequence": self.audit_sequence,
            "audit_digest": self.audit_digest,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rejection":
        return cls(
            bag_id=str(raw["bag_id"]),
            code=str(raw.get("code", "")),
            reason=str(raw.get("reason", "")),
            at=str(raw.get("at", "")),
            evidence=_clean(raw.get("evidence", {})),
            audit_sequence=int(raw.get("audit_sequence", 0)),
            audit_digest=str(raw.get("audit_digest", "")),
        )


@dataclass(frozen=True)
class LoadEntry:
    """One confirmed (or drafted) position in a loading sequence."""

    order: int
    bag_id: str
    weight: float
    position: str
    arm: float
    side: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "bag_id": self.bag_id,
            "weight": self.weight,
            "position": self.position,
            "arm": self.arm,
            "side": self.side,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadEntry":
        return cls(
            order=int(raw.get("order", 0)),
            bag_id=str(raw["bag_id"]),
            weight=float(raw.get("weight", 0.0)),
            position=str(raw.get("position", "")),
            arm=float(raw.get("arm", 0.0)),
            side=str(raw.get("side", "")),
        )


@dataclass(frozen=True)
class LoadPlanVersion:
    """An immutable version of one flight's loading plan.

    Confirmed versions are never mutated: confirming, rolling back and swapping
    all append a new version whose ``parent_version`` points at the one it
    replaced, which is what makes a swap rollback replayable and auditable.
    """

    flight_id: str
    version: int
    status: str  # draft | confirmed | superseded
    entries: tuple[LoadEntry, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    parent_version: int = 0
    created_at: str = ""
    confirmed_at: str = ""
    note: str = ""
    limits: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "version": self.version,
            "status": self.status,
            "entries": [entry.as_dict() for entry in self.entries],
            "rejections": [rejection.as_dict() for rejection in self.rejections],
            "parent_version": self.parent_version,
            "created_at": self.created_at,
            "confirmed_at": self.confirmed_at,
            "note": self.note,
            "limits": _clean(self.limits),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadPlanVersion":
        return cls(
            flight_id=str(raw["flight_id"]),
            version=int(raw.get("version", 0)),
            status=str(raw.get("status", "draft")),
            entries=tuple(LoadEntry.from_dict(item) for item in raw.get("entries", [])),
            rejections=tuple(
                Rejection.from_dict(item) for item in raw.get("rejections", [])
            ),
            parent_version=int(raw.get("parent_version", 0)),
            created_at=str(raw.get("created_at", "")),
            confirmed_at=str(raw.get("confirmed_at", "")),
            note=str(raw.get("note", "")),
            limits=_clean(raw.get("limits", {})),
        )

    def bag_ids(self) -> frozenset[str]:
        return frozenset(entry.bag_id for entry in self.entries)


@dataclass(frozen=True)
class Sequence:
    epoch: int = 0
    next_value: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next_value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sequence":
        return cls(epoch=int(raw.get("epoch", 0)), next_value=int(raw.get("next", 0)))


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_digest: str = ""
    digest: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "subject": self.subject,
            "detail": _clean(self.detail),
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            kind=str(raw.get("kind", "")),
            subject=str(raw.get("subject", "")),
            detail=_clean(raw.get("detail", {})),
            prev_digest=str(raw.get("prev_digest", "")),
            digest=str(raw.get("digest", "")),
            at=str(raw.get("at", "")),
        )
