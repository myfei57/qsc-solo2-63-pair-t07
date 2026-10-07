"""Deterministic loading-order planner.

Bounds constrain the *confirmed final load*, not every transient state while
the hold fills (the first piece on board is always laterally "unbalanced", and
a narrow CG window can make an early piece look temporarily out of range). The
planner therefore:

1. greedily places each candidate in the open slot that yields the best final
   balance (CG nearest the window centre, then smallest lateral imbalance);
2. once the whole batch is placed, checks the final set against every bound;
3. on a breach, removes the *last placed* piece — "越限时拒掉最后一件" — marks
   it refused with the numbers proving the over-limit state, and re-checks;
4. repeats until the remainder is feasible. Earlier confirmed pieces are never
   touched, so a refusal cannot silently displace an already committed piece.

The same inputs always produce the same order: scores break ties on position
id and candidates are processed in weighing/arrival order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bagsort.model.entities import LoadEntry
from . import reasons
from .constraints import BoundBreach, LoadLimits, breaches, compute_balance, open_positions


@dataclass(frozen=True)
class Candidate:
    bag_id: str
    weight: float


@dataclass(frozen=True)
class PlacementAttempt:
    position: str
    chosen: bool
    balance: dict[str, float]
    breaches: tuple[dict[str, object], ...]


@dataclass
class Refusal:
    bag_id: str
    code: str
    reason: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass
class PlanOutcome:
    entries: list[LoadEntry] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)


def _occupancy(entries: list[LoadEntry]) -> dict[str, int]:
    occupied: dict[str, int] = {}
    for entry in entries:
        occupied[entry.position] = occupied.get(entry.position, 0) + 1
    return occupied


def _placement_score(
    balance: dict[str, float], limits: LoadLimits, found: list[BoundBreach]
) -> tuple:
    mid = (limits.cg_forward + limits.cg_aft) / 2.0
    return (
        len(found),
        abs(balance["cg_arm"] - mid),
        balance["lateral_imbalance"],
    )


def place_piece(
    entries: list[LoadEntry], candidate: Candidate, limits: LoadLimits
) -> tuple[LoadEntry | None, list[PlacementAttempt]]:
    """Choose the best open slot for one piece (no hard per-step gate).

    Returns the chosen entry and, for audit evidence, every slot tried with the
    balance it would have produced. Returns ``None`` only when no slot exists.
    """

    occupied = _occupancy(entries)
    order = len(entries)
    attempts: list[PlacementAttempt] = []
    options: list[tuple[tuple, LoadEntry]] = []
    for pid, arm, side, _slots in open_positions(limits, occupied):
        trial = LoadEntry(
            order=order,
            bag_id=candidate.bag_id,
            weight=candidate.weight,
            position=pid,
            arm=arm,
            side=side,
        )
        found = breaches(entries + [trial], limits)
        balance = compute_balance(entries + [trial], limits).as_dict()
        attempts.append(
            PlacementAttempt(
                position=pid,
                chosen=False,
                balance=balance,
                breaches=tuple(breach.as_dict() for breach in found),
            )
        )
        score = _placement_score(balance, limits, found)
        options.append((score, trial, len(attempts) - 1))
    if not options:
        return None, attempts
    options.sort(key=lambda item: (*item[0], item[1].position))
    best_score, chosen, chosen_index = options[0]
    attempts[chosen_index] = PlacementAttempt(
        position=chosen.position,
        chosen=True,
        balance=attempts[chosen_index].balance,
        breaches=attempts[chosen_index].breaches,
    )
    return chosen, attempts


def _refusal_for(
    candidate: Candidate,
    limits: LoadLimits,
    entries_before: list[LoadEntry],
    attempts: list[PlacementAttempt],
) -> Refusal:
    chosen_attempt = next((a for a in attempts if a.chosen), None)
    return Refusal(
        bag_id=candidate.bag_id,
        code=reasons.PLACEMENT_BREACH,
        reason=reasons.explain(reasons.PLACEMENT_BREACH),
        evidence={
            "weight": round(candidate.weight, 3),
            "balance_without_piece": compute_balance(entries_before, limits).as_dict(),
            "balance_with_piece": chosen_attempt.balance if chosen_attempt else None,
            "breaches_with_piece": list(chosen_attempt.breaches) if chosen_attempt else [],
            "positions_tried": len(attempts),
            "attempts": [
                {
                    "position": attempt.position,
                    "balance": attempt.balance,
                    "breaches": list(attempt.breaches),
                }
                for attempt in attempts
            ],
        },
    )


def plan(
    base: list[LoadEntry],
    candidates: list[Candidate],
    limits: LoadLimits,
    quota_remaining: int = -1,
) -> PlanOutcome:
    """Build the batch, then peel the last piece until the remainder is feasible."""

    if breaches(base, limits):
        raise ValueError("base load already violates its limits; refuse to extend")

    outcome = PlanOutcome()
    remaining = quota_remaining
    staged: list[LoadEntry] = list(base)
    placed: list[tuple[Candidate, LoadEntry, list[PlacementAttempt]]] = []

    for candidate in candidates:
        if remaining == 0:
            outcome.refusals.append(
                Refusal(
                    bag_id=candidate.bag_id,
                    code=reasons.PIECE_QUOTA,
                    reason=reasons.explain(reasons.PIECE_QUOTA),
                    evidence={"quota_remaining": 0},
                )
            )
            continue
        if candidate.weight > limits.piece_weight_cap + 1e-9:
            outcome.refusals.append(
                Refusal(
                    bag_id=candidate.bag_id,
                    code=reasons.PIECE_OVERWEIGHT,
                    reason=reasons.explain(reasons.PIECE_OVERWEIGHT),
                    evidence={
                        "weight": round(candidate.weight, 3),
                        "piece_weight_cap": limits.piece_weight_cap,
                    },
                )
            )
            continue
        chosen, attempts = place_piece(staged, candidate, limits)
        if chosen is None:
            outcome.refusals.append(
                Refusal(
                    bag_id=candidate.bag_id,
                    code=reasons.PLACEMENT_BREACH,
                    reason=reasons.explain(reasons.PLACEMENT_BREACH),
                    evidence={
                        "weight": round(candidate.weight, 3),
                        "positions_tried": 0,
                        "detail": "所有舱位均已占满，无剩余槽位",
                    },
                )
            )
            continue
        staged.append(chosen)
        placed.append((candidate, chosen, attempts))
        if remaining > 0:
            remaining -= 1

    # Final-set gate: peel the last placed piece while the load is infeasible.
    while placed and breaches(staged, limits):
        candidate, _entry, attempts = placed.pop()
        staged = staged[:-1]
        outcome.refusals.append(
            _refusal_for(candidate, limits, staged, attempts)
        )

    # Re-number the newly placed batch with absolute orders continuing from the
    # base, so a later version's entries concatenate into one contiguous order.
    outcome.entries = _continuing_from(staged, len(base))
    return outcome


def _continuing_from(entries: list[LoadEntry], start: int) -> list[LoadEntry]:
    """Keep ``entries[:start]`` and give the tail contiguous orders from ``start``."""

    head = list(entries[:start])
    tail = [
        LoadEntry(
            order=start + index,
            bag_id=entry.bag_id,
            weight=entry.weight,
            position=entry.position,
            arm=entry.arm,
            side=entry.side,
        )
        for index, entry in enumerate(entries[start:])
    ]
    return head + tail
