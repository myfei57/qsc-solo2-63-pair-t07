"""Weight and balance constraints for aircraft baggage loading.

Everything here is a pure function over a :class:`LoadLimits` configuration and
a candidate entry list, so the numbers an operator sees in a rejection are the
same numbers the gate enforces. Three bounds are checked together against one
snapshot:

* **payload weight** — loaded baggage must not exceed ``weight_cap`` kg;
* **longitudinal centre of gravity** — the combined arm of the operating empty
  weight (``oew_weight`` at ``oew_arm``) and the loaded baggage must stay inside
  the inclusive ``cg_forward`` / ``cg_aft`` window;
* **lateral imbalance** — the absolute difference between the left- and
  right-side baggage moments must not exceed ``lateral_moment_cap`` kg·cm.

Weight cap and CG window are therefore properties of one and the same set: a
result is feasible only when both (and the lateral bound) hold simultaneously,
which is the invariant rollback restores after a swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from bagsort.model.entities import LoadEntry


@dataclass(frozen=True)
class LoadLimits:
    """The loadmaster configuration of one flight/aircraft."""

    weight_cap: float
    cg_forward: float
    cg_aft: float
    lateral_moment_cap: float
    piece_weight_cap: float
    oew_weight: float = 0.0
    oew_arm: float = 0.0
    positions: tuple[tuple[str, float, str, int], ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadLimits":
        positions = tuple(
            (
                str(item["id"]),
                float(item["arm"]),
                str(item.get("side", "center")),
                int(item.get("slots", 1)),
            )
            for item in raw.get("positions", [])
        )
        return cls(
            weight_cap=float(raw["weight_cap"]),
            cg_forward=float(raw["cg_forward"]),
            cg_aft=float(raw["cg_aft"]),
            lateral_moment_cap=float(raw["lateral_moment_cap"]),
            piece_weight_cap=float(raw["piece_weight_cap"]),
            oew_weight=float(raw.get("oew_weight", 0.0)),
            oew_arm=float(raw.get("oew_arm", 0.0)),
            positions=positions,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "weight_cap": self.weight_cap,
            "cg_forward": self.cg_forward,
            "cg_aft": self.cg_aft,
            "lateral_moment_cap": self.lateral_moment_cap,
            "piece_weight_cap": self.piece_weight_cap,
            "oew_weight": self.oew_weight,
            "oew_arm": self.oew_arm,
            "positions": [
                {"id": pid, "arm": arm, "side": side, "slots": slots}
                for pid, arm, side, slots in self.positions
            ],
        }

    def position(self, position_id: str) -> tuple[str, float, str, int] | None:
        for item in self.positions:
            if item[0] == position_id:
                return item
        return None


@dataclass(frozen=True)
class Balance:
    """The aggregate numbers behind every accept/refuse decision."""

    bag_weight: float
    total_weight: float
    cg_arm: float
    left_moment: float
    right_moment: float
    lateral_imbalance: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_weight": round(self.bag_weight, 3),
            "total_weight": round(self.total_weight, 3),
            "cg_arm": round(self.cg_arm, 3),
            "left_moment": round(self.left_moment, 3),
            "right_moment": round(self.right_moment, 3),
            "lateral_imbalance": round(self.lateral_imbalance, 3),
        }


def compute_balance(entries: Iterable[LoadEntry], limits: LoadLimits) -> Balance:
    """Aggregate a candidate load. Positions not on the aircraft are ignored."""

    known = {pid for pid, _, _, _ in limits.positions}
    bag_weight = 0.0
    bag_moment = 0.0
    left_moment = 0.0
    right_moment = 0.0
    for entry in entries:
        if entry.position not in known:
            continue
        bag_weight += entry.weight
        bag_moment += entry.weight * entry.arm
        if entry.side == "left":
            left_moment += entry.weight * entry.arm
        elif entry.side == "right":
            right_moment += entry.weight * entry.arm

    total_weight = limits.oew_weight + bag_weight
    total_moment = limits.oew_weight * limits.oew_arm + bag_moment
    cg_arm = total_moment / total_weight if total_weight > 0 else limits.oew_arm
    return Balance(
        bag_weight=bag_weight,
        total_weight=total_weight,
        cg_arm=cg_arm,
        left_moment=left_moment,
        right_moment=right_moment,
        lateral_imbalance=abs(left_moment - right_moment),
    )


@dataclass(frozen=True)
class BoundBreach:
    code: str
    bound: str
    observed: float
    limit: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "bound": self.bound,
            "observed": round(self.observed, 3),
            "limit": round(self.limit, 3),
        }


def breaches(entries: list[LoadEntry], limits: LoadLimits) -> list[BoundBreach]:
    """Every bound the candidate set violates. Empty list means it flies."""

    balance = compute_balance(entries, limits)
    found: list[BoundBreach] = []
    if balance.bag_weight > limits.weight_cap + 1e-9:
        found.append(
            BoundBreach(
                code="WEIGHT_OVER_CAP",
                bound="baggage_payload",
                observed=balance.bag_weight,
                limit=limits.weight_cap,
            )
        )
    if balance.cg_arm < limits.cg_forward - 1e-9:
        found.append(
            BoundBreach(
                code="CG_TOO_FORWARD",
                bound="cg_forward",
                observed=balance.cg_arm,
                limit=limits.cg_forward,
            )
        )
    if balance.cg_arm > limits.cg_aft + 1e-9:
        found.append(
            BoundBreach(
                code="CG_TOO_AFT",
                bound="cg_aft",
                observed=balance.cg_arm,
                limit=limits.cg_aft,
            )
        )
    if balance.lateral_imbalance > limits.lateral_moment_cap + 1e-9:
        found.append(
            BoundBreach(
                code="LATERAL_IMBALANCE",
                bound="lateral_moment",
                observed=balance.lateral_imbalance,
                limit=limits.lateral_moment_cap,
            )
        )
    return found


def feasible(entries: list[LoadEntry], limits: LoadLimits) -> bool:
    return not breaches(entries, limits)


def capacity_for(
    limits: LoadLimits, occupied: dict[str, int], position_id: str
) -> int:
    item = limits.position(position_id)
    if item is None:
        return 0
    return max(0, item[3] - occupied.get(position_id, 0))


def open_positions(
    limits: LoadLimits, occupied: dict[str, int]
) -> list[tuple[str, float, str, int]]:
    """Positions that still have at least one free slot."""

    return [
        (pid, arm, side, slots)
        for pid, arm, side, slots in limits.positions
        if capacity_for(limits, occupied, pid) > 0
    ]
