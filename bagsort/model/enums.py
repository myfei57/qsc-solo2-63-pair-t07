"""Enumerations for the two state machines the plant tracks."""

from __future__ import annotations

from enum import Enum


class FlightState(str, Enum):
    """Lifecycle of one flight batch."""

    OPEN = "open"
    BOARDING = "boarding"
    CUTOFF = "cutoff"
    CLOSED = "closed"


class BagState(str, Enum):
    """Lifecycle of one piece of baggage."""

    CHECKED_IN = "checked_in"
    INJECTED = "injected"
    SORTED = "sorted"
    RECHECKED = "rechecked"
    LOADED = "loaded"
    DIVERTED = "diverted"
    WEIGHED = "weighed"
    LOAD_REJECTED = "load_rejected"
    LOAD_SWAPPED_OUT = "load_swapped_out"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.SORTED,
            BagState.RECHECKED,
            BagState.WEIGHED,
        )

    @property
    def loadable(self) -> bool:
        """Whether the piece may be admitted into a load plan.

        Only a weighed, not-yet-loaded piece counts. A rejected piece stays
        rejected until a new weighing produces a new decision, and a piece
        swapped out of a confirmed plan is retired from the counted set.
        """

        return self is BagState.WEIGHED

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"
