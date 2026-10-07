"""The per-flight weight ledger ("额度账").

Postings are append-only signed movements. The ledger is the authority for
"how much loaded weight is currently charged to a flight", and because each
posting names a ``bag_id`` it also answers "is this piece already counted in a
batch". The loadmaster cross-foots the ledger against the confirmed plan on
every verify/export, so a bag counted twice or a posting without a plan entry
would be detected rather than shipped.
"""

from __future__ import annotations

from collections import defaultdict

from bagsort.model.entities import WeightPosting
from bagsort.store.store import Store


class WeightLedger:
    """Read-side projection and writer of the signed weight account."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def postings(self, flight_id: str | None = None) -> list[WeightPosting]:
        postings = self._store.state().weight_postings
        if flight_id is None:
            return list(postings)
        return [posting for posting in postings if posting.flight_id == flight_id]

    def balance(self, flight_id: str) -> float:
        return round(
            sum(posting.delta for posting in self.postings(flight_id)), 6
        )

    def net_by_bag(self, flight_id: str) -> dict[str, float]:
        """Signed net charged weight per bag; a fully released bag nets zero."""

        totals: dict[str, float] = defaultdict(float)
        for posting in self.postings(flight_id):
            totals[posting.bag_id] += posting.delta
        return {bag_id: round(weight, 6) for bag_id, weight in totals.items()}

    def counted_bags(self, flight_id: str) -> frozenset[str]:
        """Bags whose net charged weight is strictly positive.

        A bag released during rollback/swap nets to zero and therefore drops out
        of the counted set — it can legitimately be weighed and loaded again,
        while a bag still sitting in a confirmed plan stays counted and is
        refused with ``DUPLICATE_BATCH`` if another batch tries to count it.
        """

        return frozenset(
            bag_id
            for bag_id, net in self.net_by_bag(flight_id).items()
            if net > 1e-9
        )
