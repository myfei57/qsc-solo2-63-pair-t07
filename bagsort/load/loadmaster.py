"""Loadmaster: weigh, sequence by weight-and-balance, refuse, swap and roll back.

This subsystem sits between the recheck queue and the carousel. It owns four
ledgers that must stay mutually reconcilable:

* the **weighing record** (append-only scale readings);
* the **weight ledger** (signed posted kg per flight and per bag);
* the **loading plan versions** (immutable sequences with the refusals each
  run produced);
* the **tamper-evident audit chain** (every refusal, confirm, rollback and
  swap, including attempted swaps that failed).

All mutations go through the append-only journal, so restart by replay rebuilds
exactly the same ledgers. Every state-changing method here is a single store
session: an exception drops the whole transaction, which is the mechanism that
makes a failed swap leave the confirmed sequence byte-for-byte unchanged.

Version shapes:

* a *confirm* of a submitted batch stores only that batch's entries and points
  ``parent_version`` at the previously confirmed plan;
* a *rollback* or *swap* stores the full resulting entry list (a revision).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.model.entities import (
    Bag,
    LoadEntry,
    LoadPlanVersion,
    Rejection,
    WeighRecord,
    WeightPosting,
)
from bagsort.model.enums import BagState
from bagsort.store.store import Store
from . import reasons
from .constraints import Balance, LoadLimits, breaches, compute_balance, feasible
from .ledger import WeightLedger
from .planner import Candidate, Refusal, place_piece, plan as run_plan


@dataclass(frozen=True)
class WeighResult:
    record: WeighRecord
    accepted: bool
    bag: Bag | None


class LoadMaster:
    """The operator-facing weight-and-balance service for one plant."""

    def __init__(self, store: Store, clock: Clock, audit: object, book: object) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._book = book
        self.ledger = WeightLedger(store)

    @property
    def store(self) -> Store:
        return self._store

    # -- configuration -----------------------------------------------------

    def configure(
        self,
        flight_id: str,
        limits: LoadLimits | dict[str, Any],
    ) -> LoadLimits:
        """Set (and journal) the weight/CG configuration of a flight."""

        self._require_flight(flight_id)
        resolved = (
            limits if isinstance(limits, LoadLimits) else LoadLimits.from_dict(limits)
        )
        self._validate_limits(resolved)
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.load_limits[flight_id] = resolved.as_dict()
            session.record(
                "loadlimits.set",
                {"flight_id": flight_id, "limits": resolved.as_dict()},
                at,
            )
        self._audit.record(
            "load_limits_configured",
            flight_id,
            "weigh",
            {
                "weight_cap": resolved.weight_cap,
                "piece_weight_cap": resolved.piece_weight_cap,
                "cg_forward": resolved.cg_forward,
                "cg_aft": resolved.cg_aft,
                "lateral_moment_cap": resolved.lateral_moment_cap,
                "positions": len(resolved.positions),
            },
        )
        return resolved

    @staticmethod
    def _validate_limits(limits: LoadLimits) -> None:
        if limits.weight_cap <= 0:
            raise ValidationError("weight_cap must be positive")
        if limits.piece_weight_cap <= 0:
            raise ValidationError("piece_weight_cap must be positive")
        if limits.cg_forward > limits.cg_aft:
            raise ValidationError("cg_forward must not be greater than cg_aft")
        if limits.lateral_moment_cap < 0:
            raise ValidationError("lateral_moment_cap must not be negative")
        normalised: list[tuple] = []
        for item in limits.positions:
            if not isinstance(item, tuple) or len(item) != 4:
                raise ValidationError(
                    "each position must be a (id, arm, side, slots) tuple"
                )
            pid, arm, side, slots = item
            if not str(pid).strip():
                raise ValidationError("position id must not be empty")
            if not isinstance(slots, int) or slots <= 0:
                raise ValidationError("position slots must be a positive integer")
            if side not in {"left", "right", "center"}:
                raise ValidationError(f"unsupported position side {side!r}")
            normalised.append((str(pid), float(arm), str(side), slots))
        ids = [pid for pid, _, _, _ in normalised]
        if len(ids) != len(set(ids)):
            raise ValidationError("position ids must be unique")
        if not ids:
            raise ValidationError("at least one hold position is required")

    def limits(self, flight_id: str) -> LoadLimits:
        raw = self._store.state().load_limits.get(flight_id)
        if raw is None:
            raise NotFoundError(f"flight {flight_id!r} has no load limits configured")
        return LoadLimits.from_dict(raw)

    def has_limits(self, flight_id: str) -> bool:
        return flight_id in self._store.state().load_limits

    # -- weighing ----------------------------------------------------------

    def weigh(
        self,
        bag_id: str,
        weight: float,
        scale_id: str = "SC1",
        flight_id: str | None = None,
    ) -> WeighResult:
        """Record one scale reading. Bad readings are logged but never counted.

        A piece with no valid reading is the "缺称重" case: it is refused at the
        scale and may only proceed after a fresh accepted re-weigh. A piece
        already counted in a confirmed plan cannot be re-weighed — that is the
        same identity counted twice by another route.
        """

        bag = self._require_bag(bag_id)
        target_flight = (
            flight_id
            or self._flight_of(bag_id)
            or (bag.flight_id or None)
            or ""
        )
        if not target_flight:
            raise ValidationError(f"bag {bag_id!r} is not mapped to a flight")
        if bag.state is BagState.LOADED:
            raise ConflictError(
                f"bag {bag_id!r} is already counted in a confirmed load"
            )
        try:
            value = float(weight)
        except (TypeError, ValueError) as exc:
            raise ValidationError("weight must be a number") from exc

        at = self._clock.now_iso()
        if value <= 0:
            record = WeighRecord(
                bag_id=bag_id,
                flight_id=target_flight,
                weight=value,
                scale_id=scale_id,
                at=at,
                accepted=False,
                reason=reasons.BAD_WEIGHING,
            )
            self._commit_weigh(record, at)
            self._audit.record(
                "weigh_rejected",
                bag_id,
                "weigh",
                {"weight": value, "scale_id": scale_id, "code": reasons.BAD_WEIGHING},
            )
            return WeighResult(record=record, accepted=False, bag=bag)

        piece_cap = (
            self.limits(target_flight).piece_weight_cap
            if self.has_limits(target_flight)
            else None
        )
        if piece_cap is not None and value > piece_cap + 1e-9:
            record = WeighRecord(
                bag_id=bag_id,
                flight_id=target_flight,
                weight=value,
                scale_id=scale_id,
                at=at,
                accepted=False,
                reason=reasons.PIECE_OVERWEIGHT,
            )
            self._commit_weigh(record, at)
            rejected = bag.evolved(state=BagState.LOAD_REJECTED)
            with self._store.session() as session:
                session.state.bags[bag_id] = rejected
                session.record("bag.put", {"bag": rejected.as_dict()}, at)
            self._audit.record(
                "weigh_rejected",
                bag_id,
                "weigh",
                {
                    "weight": round(value, 3),
                    "piece_weight_cap": piece_cap,
                    "scale_id": scale_id,
                    "code": reasons.PIECE_OVERWEIGHT,
                },
            )
            return WeighResult(record=record, accepted=False, bag=rejected)

        record = WeighRecord(
            bag_id=bag_id,
            flight_id=target_flight,
            weight=value,
            scale_id=scale_id,
            at=at,
            accepted=True,
        )
        self._commit_weigh(record, at)
        weighed = bag.evolved(state=BagState.WEIGHED, flight_id=target_flight)
        with self._store.session() as session:
            session.state.bags[bag_id] = weighed
            session.record("bag.put", {"bag": weighed.as_dict()}, at)
        self._audit.record(
            "weighed",
            bag_id,
            "weigh",
            {
                "weight": round(value, 3),
                "scale_id": scale_id,
                "flight_id": target_flight,
            },
        )
        return WeighResult(record=record, accepted=True, bag=weighed)

    def _commit_weigh(self, record: WeighRecord, at: str) -> None:
        with self._store.session() as session:
            session.state.weigh_records.append(record)
            session.record("weigh.append", {"record": record.as_dict()}, at)

    def weighings(self, bag_id: str | None = None) -> list[WeighRecord]:
        records = self._store.state().weigh_records
        if bag_id is None:
            return list(records)
        return [record for record in records if record.bag_id == bag_id]

    def accepted_weight(self, bag_id: str) -> float | None:
        """Latest accepted reading for a bag, or ``None`` when never weighed."""

        accepted = [record for record in self.weighings(bag_id) if record.accepted]
        return accepted[-1].weight if accepted else None

    # -- planning ----------------------------------------------------------

    def submit_batch(
        self,
        flight_id: str,
        bag_ids: list[str],
        batch: int | None = None,
    ) -> LoadPlanVersion:
        """Offer one batch; build a draft refusing whatever cannot fly.

        Missing weighings, duplicates and un-loadable states become refusal
        records on the draft rather than exceptions, so a batch of mixed
        readiness still returns a fully explained plan.
        """

        limits = self.limits(flight_id)
        at = self._clock.now_iso()
        confirmed = self.confirmed(flight_id)
        base_entries = self.sequence(flight_id)
        counted = self.ledger.counted_bags(flight_id) | {
            entry.bag_id for entry in base_entries
        }

        candidates: list[Candidate] = []
        pre_refusals: list[Refusal] = []
        seen: set[str] = set()
        state = self._store.state()
        for bag_id in bag_ids:
            bag = state.bags.get(bag_id)
            if bag is None:
                pre_refusals.append(
                    Refusal(bag_id, reasons.NOT_LOADABLE, "系统内无此行李记录", {})
                )
                continue
            if bag_id in seen or bag_id in counted:
                pre_refusals.append(
                    Refusal(
                        bag_id,
                        reasons.DUPLICATE_BATCH,
                        reasons.explain(reasons.DUPLICATE_BATCH),
                        {"already_in_version": confirmed.version if confirmed else 0},
                    )
                )
                continue
            seen.add(bag_id)
            weight = self.accepted_weight(bag_id)
            if weight is None:
                # 缺称重处置：扣留为待复秤件，不计入任何重量，复秤后重新报批。
                # 先看“有没有秤”再看状态，因此值机后未过秤的件落到这里，
                # 而不是被笼统地判成状态不符。
                pre_refusals.append(
                    Refusal(
                        bag_id,
                        reasons.MISSING_WEIGHING,
                        reasons.explain(reasons.MISSING_WEIGHING),
                        {
                            "held": True,
                            "state": bag.state.value,
                            "disposition": "recheck_scale_then_resubmit",
                        },
                    )
                )
                continue
            if not bag.state.loadable:
                pre_refusals.append(
                    Refusal(
                        bag_id,
                        reasons.NOT_LOADABLE,
                        reasons.explain(reasons.NOT_LOADABLE),
                        {"state": bag.state.value},
                    )
                )
                continue
            candidates.append(Candidate(bag_id=bag_id, weight=weight))

        outcome = run_plan(base_entries, candidates, limits)
        new_entries = outcome.entries[len(base_entries) :]
        rejections = tuple(self._to_rejections(pre_refusals + outcome.refusals, at))

        draft = LoadPlanVersion(
            flight_id=flight_id,
            version=self._next_version(flight_id),
            status="draft",
            entries=tuple(new_entries),
            rejections=rejections,
            parent_version=confirmed.version if confirmed else 0,
            created_at=at,
            note=f"batch={batch if batch is not None else ''}",
            limits=limits.as_dict(),
        )
        with self._store.session() as session:
            session.state.load_plans.setdefault(flight_id, []).append(draft)
            session.record("loadplan.append", {"version": draft.as_dict()}, at)
        return draft

    def confirm(self, flight_id: str, version: int | None = None) -> LoadPlanVersion:
        """Confirm a draft in one transaction: audit refusals, post weight, flip.

        Weight postings, bag states, refusal audit rows and the plan status
        change commit together, so the weight ledger and the confirmed sequence
        can never be observed disagreeing.
        """

        draft = self._draft(flight_id, version)
        limits = LoadLimits.from_dict(draft.limits)
        at = self._clock.now_iso()
        combined = self.sequence(flight_id) + list(draft.entries)
        if not feasible(combined, limits):
            raise ConflictError("refusing to confirm an infeasible load plan")

        stamped: list[Rejection] = []
        with self._store.session() as session:
            for rejection in draft.rejections:
                entry = self._audit.build_entry(
                    "load_rejected",
                    rejection.bag_id,
                    "weigh",
                    {
                        "code": rejection.code,
                        "reason": rejection.reason,
                        "flight_id": flight_id,
                        "evidence": rejection.evidence,
                    },
                    at,
                )
                # build_entry reads committed state; earlier rows in this same
                # transaction must be linked explicitly so the chain stays valid.
                entry = self._relink(session, entry)
                self._audit.stamp(session, entry)
                stamped.append(
                    Rejection(
                        bag_id=rejection.bag_id,
                        code=rejection.code,
                        reason=rejection.reason,
                        at=rejection.at,
                        evidence=rejection.evidence,
                        audit_sequence=entry.sequence,
                        audit_digest=entry.digest,
                    )
                )

            for entry in draft.entries:
                posting = WeightPosting(
                    flight_id=flight_id,
                    bag_id=entry.bag_id,
                    delta=entry.weight,
                    kind="load",
                    plan_version=draft.version,
                    at=at,
                    ref=f"v{draft.version}#{entry.order}",
                )
                session.state.weight_postings.append(posting)
                session.record("weight.post", {"posting": posting.as_dict()}, at)
                bag = session.state.bags.get(entry.bag_id)
                if bag is not None:
                    updated = bag.evolved(state=BagState.LOADED)
                    session.state.bags[entry.bag_id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)

            session.record(
                "loadplan.confirm",
                {
                    "flight_id": flight_id,
                    "version": draft.version,
                    "confirmed_at": at,
                    "rejections": [rejection.as_dict() for rejection in stamped],
                },
                at,
            )

        combined = self.sequence(flight_id)
        self._audit.record(
            "load_confirmed",
            flight_id,
            "belt",
            {
                "version": draft.version,
                "parent_version": draft.parent_version,
                "pieces": len(draft.entries),
                "loaded_weight": round(sum(e.weight for e in draft.entries), 3),
                "refusals": len(stamped),
                "balance": compute_balance(combined, limits).as_dict(),
            },
            at,
        )
        return self.confirmed(flight_id)

    @staticmethod
    def _relink(session: object, entry: object) -> object:
        """Re-number/re-link a prebuilt audit row against rows in the session.

        ``AuditChain.build_entry`` reads committed state; inside one transaction
        a second row must chain off the first staged row, not off the same
        committed predecessor.
        """

        from bagsort.audit.chain import GENESIS, digest_of
        from bagsort.model.entities import AuditEntry

        audit = session.state.audit
        previous = audit[-1].digest if audit else GENESIS
        candidate = AuditEntry(
            sequence=len(audit) + 1,
            kind=entry.kind,
            subject=entry.subject,
            detail=entry.detail,
            prev_digest=previous,
            digest="",
            at=entry.at,
        )
        return AuditEntry(
            sequence=candidate.sequence,
            kind=candidate.kind,
            subject=candidate.subject,
            detail=candidate.detail,
            prev_digest=candidate.prev_digest,
            digest=digest_of(candidate),
            at=candidate.at,
        )

    # -- rollback and swap -------------------------------------------------

    def rollback_to(self, flight_id: str, version: int) -> LoadPlanVersion:
        """Undo a confirmed suffix back to (and keeping) an ancestor version.

        Releases the weight postings of every undone entry as ``-weight`` and
        returns the affected pieces to WEIGHED. The restored snapshot is
        re-checked for *both* weight cap and CG window before commit, so a
        rollback only ever lands on a state where the two hold together.
        """

        confirmed = self.confirmed(flight_id)
        if confirmed is None:
            raise ConflictError("no confirmed plan to roll back")
        if version != 0 and not self._is_ancestor(flight_id, version, confirmed.version):
            raise ConflictError(reasons.explain(reasons.ROLLBACK_STALE))

        as_of = self._entries_as_of(flight_id, version)
        current = self.sequence(flight_id)
        if {e.bag_id for e in as_of} == {e.bag_id for e in current} and len(as_of) == len(
            current
        ):
            raise ConflictError("rollback target equals the current sequence")
        limits = self.limits(flight_id)
        if not feasible(as_of, limits):
            raise ConflictError("rollback target violates weight or CG bounds")
        return self._commit_revision(
            flight_id=flight_id,
            result=as_of,
            audit_kind="load_rolled_back",
            stage="belt",
            status_note=f"rollback_to=v{version}",
            detail_extra={"rolled_back_to": version},
        )

    def swap(self, flight_id: str, out_bag_id: str, in_bag_id: str) -> LoadPlanVersion:
        """Replace one confirmed piece with a lighter, weighed piece.

        Compensating, auditable, and all-or-nothing:

        1. validate every precondition against the committed state — nothing is
           touched when these fail;
        2. conceptually unload the confirmed suffix after the removed piece in
           LIFO order (the physical order crews unload in);
        3. place the lighter replacement, then re-place the suffix;
        4. commit only if the result satisfies weight cap **and** CG window
           simultaneously; otherwise the pre-swap snapshot stays authoritative
           and the failed attempt is recorded.
        """

        confirmed = self.confirmed(flight_id)
        if confirmed is None:
            raise ConflictError(reasons.explain(reasons.SWAP_PRECONDITION))
        limits = self.limits(flight_id)
        state = self._store.state()

        current = self.sequence(flight_id)
        out_entry = next(
            (entry for entry in current if entry.bag_id == out_bag_id), None
        )
        if out_entry is None:
            raise ConflictError(f"换装目标 {out_bag_id!r} 不在已确认序列中")
        in_bag = state.bags.get(in_bag_id)
        if in_bag is None or not in_bag.state.loadable:
            raise ConflictError(reasons.explain(reasons.SWAP_PRECONDITION))
        in_weight = self.accepted_weight(in_bag_id)
        if in_weight is None:
            raise ConflictError(reasons.explain(reasons.MISSING_WEIGHING))
        if in_bag_id in self.ledger.counted_bags(flight_id):
            raise ConflictError(reasons.explain(reasons.DUPLICATE_BATCH))
        if in_weight >= out_entry.weight - 1e-9:
            raise ConflictError(
                f"换装件必须严格更轻：在件 {in_weight:g}kg ≥ 卸下件 {out_entry.weight:g}kg"
            )

        cut_index = next(
            index for index, entry in enumerate(current) if entry.bag_id == out_bag_id
        )
        prefix = current[:cut_index]
        suffix = current[cut_index + 1 :]

        chosen, attempts = place_piece(
            prefix, Candidate(bag_id=in_bag_id, weight=in_weight), limits
        )
        if chosen is None:
            self._record_failed_swap(
                flight_id,
                {
                    "out_bag": out_bag_id,
                    "in_bag": in_bag_id,
                    "code": reasons.SWAP_INFEASIBLE,
                    "reason": reasons.explain(reasons.SWAP_INFEASIBLE),
                    "attempts": [
                        {"position": a.position, "breaches": list(a.breaches)}
                        for a in attempts
                    ],
                },
            )
            raise ConflictError(reasons.explain(reasons.SWAP_INFEASIBLE))

        working = prefix + [chosen]
        re_placed: list[str] = []
        blocked: str | None = None
        for entry in suffix:
            replanned, _attempts = place_piece(
                working, Candidate(bag_id=entry.bag_id, weight=entry.weight), limits
            )
            if replanned is None:
                blocked = entry.bag_id
                break
            working.append(replanned)
            re_placed.append(entry.bag_id)

        # The bounds constrain the final set. The greedy step above optimises
        # placement, but the swap is only acceptable when the whole resulting
        # load satisfies weight cap and CG window *simultaneously*. Re-check
        # before any mutation; a failure leaves the confirmed sequence intact.
        final_breaches = breaches(working, limits)
        if blocked is not None or final_breaches:
            detail = {
                "out_bag": out_bag_id,
                "in_bag": in_bag_id,
                "code": reasons.SWAP_INFEASIBLE,
                "reason": reasons.explain(reasons.SWAP_INFEASIBLE),
                "balance_after_attempt": compute_balance(working, limits).as_dict(),
                "breaches": [breach.as_dict() for breach in final_breaches],
            }
            if blocked is not None:
                detail["blocked_bag"] = blocked
            self._record_failed_swap(flight_id, detail)
            raise ConflictError(reasons.explain(reasons.SWAP_INFEASIBLE))

        return self._commit_revision(
            flight_id=flight_id,
            result=working,
            audit_kind="load_swapped",
            stage="belt",
            status_note=f"swap:{out_bag_id}->{in_bag_id}",
            detail_extra={
                "out_bag": out_bag_id,
                "out_weight": round(out_entry.weight, 3),
                "in_bag": in_bag_id,
                "in_weight": round(in_weight, 3),
                "re_placed": re_placed,
            },
        )

    # -- read side ---------------------------------------------------------

    def versions(self, flight_id: str) -> list[LoadPlanVersion]:
        return list(self._store.state().load_plans.get(flight_id, []))

    def confirmed(self, flight_id: str) -> LoadPlanVersion | None:
        confirmed_versions = [
            plan_version
            for plan_version in self.versions(flight_id)
            if plan_version.status == "confirmed"
        ]
        return confirmed_versions[-1] if confirmed_versions else None

    def draft(self, flight_id: str) -> LoadPlanVersion | None:
        drafts = [
            plan_version
            for plan_version in self.versions(flight_id)
            if plan_version.status == "draft"
        ]
        return drafts[-1] if drafts else None

    def sequence(self, flight_id: str) -> list[LoadEntry]:
        """The current confirmed loading order to hand the carousel."""

        confirmed = self.confirmed(flight_id)
        return self._cumulative(confirmed) if confirmed else []

    def refusals(self, flight_id: str) -> list[Rejection]:
        confirmed = self.confirmed(flight_id)
        return list(confirmed.rejections) if confirmed else []

    def balance(self, flight_id: str) -> Balance:
        return compute_balance(self.sequence(flight_id), self.limits(flight_id))

    def loading_report(self, flight_id: str) -> dict[str, Any]:
        """One consistent snapshot: order, balance, ledger and refusals."""

        limits = self.limits(flight_id)
        entries = self.sequence(flight_id)
        found = breaches(entries, limits)
        confirmed = self.confirmed(flight_id)
        return {
            "flight_id": flight_id,
            "version": confirmed.version if confirmed else 0,
            "limits": limits.as_dict(),
            "sequence": [entry.as_dict() for entry in entries],
            "balance": compute_balance(entries, limits).as_dict(),
            "breaches": [breach.as_dict() for breach in found],
            "feasible": not found,
            "ledger_balance": self.ledger.balance(flight_id),
            "counted_bags": sorted(self.ledger.counted_bags(flight_id)),
            "refusals": [rejection.as_dict() for rejection in self.refusals(flight_id)],
        }

    # -- verification -------------------------------------------------------

    def verify_flight(self, flight_id: str) -> dict[str, Any]:
        """Cross-foot the four ledgers; returns per-check findings."""

        checks: dict[str, Any] = {}
        limits = self.limits(flight_id)
        entries = self.sequence(flight_id)

        net = self.ledger.net_by_bag(flight_id)
        plan_weight: dict[str, float] = {}
        for entry in entries:
            plan_weight[entry.bag_id] = round(
                plan_weight.get(entry.bag_id, 0.0) + entry.weight, 6
            )
        mismatched = {
            bag_id: round(net.get(bag_id, 0.0) - plan_weight.get(bag_id, 0.0), 6)
            for bag_id in set(net) | set(plan_weight)
            if abs(net.get(bag_id, 0.0) - plan_weight.get(bag_id, 0.0)) > 1e-6
        }
        ledger_total = self.ledger.balance(flight_id)
        plan_total = round(sum(plan_weight.values()), 6)
        checks["weight_ledger_matches_plan"] = not mismatched and abs(
            ledger_total - plan_total
        ) <= 1e-6
        checks["ledger_total"] = ledger_total
        checks["plan_total"] = plan_total
        checks["per_bag_delta"] = mismatched

        ids = [entry.bag_id for entry in entries]
        checks["no_duplicate_bag"] = len(ids) == len(set(ids))

        found = breaches(entries, limits)
        checks["within_weight_and_cg"] = not found
        checks["breaches"] = [breach.as_dict() for breach in found]

        audit_sequences = {entry.sequence: entry.digest for entry in self._audit.entries()}
        checks["refusals_audited"] = all(
            rejection.audit_sequence in audit_sequences
            and audit_sequences.get(rejection.audit_sequence) == rejection.audit_digest
            for rejection in self.refusals(flight_id)
        )
        checks["audit_chain_intact"] = self._audit.verify()
        checks["ok"] = all(value for key, value in checks.items() if isinstance(value, bool))
        return checks

    # -- internals ----------------------------------------------------------

    def _record_failed_swap(self, flight_id: str, detail: dict[str, Any]) -> None:
        """Audit an attempted swap that changed nothing.

        A feasibility failure mutates no plan or ledger rows, so its audit entry
        is deliberately a separate committed record: the entry itself is the
        evidence that the confirmed sequence was left intact.
        """

        self._audit.record("load_swap_failed", flight_id, "belt", detail)

    def _commit_revision(
        self,
        flight_id: str,
        result: list[LoadEntry],
        audit_kind: str,
        stage: str,
        status_note: str,
        detail_extra: dict[str, Any],
    ) -> LoadPlanVersion:
        """Append a confirmed revision, posting the signed weight delta.

        Postings come from the symmetric difference against the previous
        confirmed plan: a rollback suffix releases its weight once; a swap
        releases the removed piece and charges the replacement. Bag states move
        with the postings, all in one transaction.
        """

        confirmed = self.confirmed(flight_id)
        limits = self.limits(flight_id)
        previous = self.sequence(flight_id)
        if not feasible(result, limits):
            raise ConflictError("revision violates weight or CG bounds")

        previous_by_bag = {entry.bag_id: entry for entry in previous}
        result_by_bag = {entry.bag_id: entry for entry in result}
        at = self._clock.now_iso()
        new_version_no = self._next_version(flight_id)

        revision = LoadPlanVersion(
            flight_id=flight_id,
            version=new_version_no,
            status="confirmed",
            entries=tuple(result),
            rejections=confirmed.rejections if confirmed else (),
            parent_version=confirmed.version if confirmed else 0,
            created_at=at,
            confirmed_at=at,
            note=status_note,
            limits=limits.as_dict(),
        )
        with self._store.session() as session:
            for bag_id in previous_by_bag.keys() - result_by_bag.keys():
                old = previous_by_bag[bag_id]
                posting = WeightPosting(
                    flight_id=flight_id,
                    bag_id=bag_id,
                    delta=-old.weight,
                    kind="release",
                    plan_version=new_version_no,
                    at=at,
                    ref=status_note,
                )
                session.state.weight_postings.append(posting)
                session.record("weight.post", {"posting": posting.as_dict()}, at)
                bag = session.state.bags.get(bag_id)
                if bag is not None:
                    updated = bag.evolved(state=BagState.WEIGHED)
                    session.state.bags[bag_id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)
            for bag_id in result_by_bag.keys() - previous_by_bag.keys():
                new = result_by_bag[bag_id]
                posting = WeightPosting(
                    flight_id=flight_id,
                    bag_id=bag_id,
                    delta=new.weight,
                    kind="load",
                    plan_version=new_version_no,
                    at=at,
                    ref=status_note,
                )
                session.state.weight_postings.append(posting)
                session.record("weight.post", {"posting": posting.as_dict()}, at)
                bag = session.state.bags.get(bag_id)
                if bag is not None:
                    updated = bag.evolved(state=BagState.LOADED)
                    session.state.bags[bag_id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)
            session.state.load_plans.setdefault(flight_id, []).append(revision)
            session.record("loadplan.append", {"version": revision.as_dict()}, at)

        detail = {
            "version": new_version_no,
            "parent_version": confirmed.version if confirmed else 0,
            "pieces": len(result),
            "ledger_balance": round(sum(entry.weight for entry in result), 3),
        }
        detail.update(detail_extra)
        self._audit.record(audit_kind, flight_id, stage, detail, at)
        return revision

    def _entries_as_of(self, flight_id: str, version: int) -> list[LoadEntry]:
        if version == 0:
            return []
        target = next(
            (
                plan_version
                for plan_version in self.versions(flight_id)
                if plan_version.version == version and plan_version.status == "confirmed"
            ),
            None,
        )
        return self._cumulative(target) if target is not None else []

    def _cumulative(self, plan_version: LoadPlanVersion | None) -> list[LoadEntry]:
        """Resolve a version to its full entry list, following parent links."""

        if plan_version is None:
            return []
        is_revision = plan_version.note.startswith(("rollback", "swap"))
        if plan_version.parent_version == 0 or is_revision:
            return list(plan_version.entries)
        parent = next(
            (
                item
                for item in self.versions(plan_version.flight_id)
                if item.version == plan_version.parent_version
            ),
            None,
        )
        return self._cumulative(parent) + list(plan_version.entries)

    def _is_ancestor(self, flight_id: str, ancestor: int, current: int) -> bool:
        if ancestor == current:
            return True
        seen: set[int] = set()
        probe = current
        while probe and probe not in seen:
            seen.add(probe)
            plan_version = next(
                (item for item in self.versions(flight_id) if item.version == probe),
                None,
            )
            if plan_version is None:
                return False
            if plan_version.parent_version == ancestor:
                return True
            if plan_version.parent_version == 0:
                return False
            probe = plan_version.parent_version
        return False

    def _draft(self, flight_id: str, version: int | None) -> LoadPlanVersion:
        drafts = [
            plan_version
            for plan_version in self.versions(flight_id)
            if plan_version.status == "draft"
        ]
        if not drafts:
            raise ConflictError(f"flight {flight_id!r} has no draft plan")
        if version is None:
            return drafts[-1]
        target = next(
            (plan_version for plan_version in drafts if plan_version.version == version),
            None,
        )
        if target is None:
            raise ConflictError(f"draft version {version} not found")
        return target

    def _next_version(self, flight_id: str) -> int:
        versions = self._store.state().load_plans.get(flight_id, [])
        return max((plan_version.version for plan_version in versions), default=0) + 1

    @staticmethod
    def _to_rejections(refusals: list[Refusal], at: str) -> list[Rejection]:
        return [
            Rejection(
                bag_id=refusal.bag_id,
                code=refusal.code,
                reason=refusal.reason,
                at=at,
                evidence=dict(refusal.evidence),
            )
            for refusal in refusals
        ]

    def _require_flight(self, flight_id: str) -> None:
        if flight_id not in self._store.state().flights:
            raise NotFoundError(f"flight {flight_id!r} not found")

    def _require_bag(self, bag_id: str) -> Bag:
        bag = self._store.state().bags.get(bag_id)
        if bag is None:
            raise NotFoundError(f"bag {bag_id!r} not found")
        return bag

    def _flight_of(self, bag_id: str) -> str | None:
        return self._store.state().mappings.get(bag_id)
