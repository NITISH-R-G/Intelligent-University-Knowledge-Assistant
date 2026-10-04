"""Time as an injected collaborator.

Why this abstraction earns its place (Phase 0 review Rule 3): the Phase 1 job runner has
lease expiry, retry backoff and idempotency retention - all time-dependent. If they read the
system clock directly, every test either sleeps or becomes a test of the machine's speed. With
``Clock`` injected, ``FixedClock`` makes "a lease expired 61 seconds ago" a single line.

Time is an *input* to business logic, not an ambient fact. Treating it that way is the
difference between testing lease expiry and never testing it.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "FixedClock", "UTC"]

#: All timestamps in this system are timezone-aware UTC. Naive datetimes are a bug source:
#: a naive timestamp compared to an aware one raises, and a naive one stored in Postgres
#: is interpreted in the server's local zone, which changes when the server moves.
UTC = dt.UTC


@runtime_checkable
class Clock(Protocol):
    """Source of the current time."""

    def now(self) -> dt.datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Production clock reading the host system time in UTC."""

    __slots__ = ()

    def now(self) -> dt.datetime:
        """Return the current UTC time, microsecond resolution."""
        return dt.datetime.now(tz=UTC)

    def __repr__(self) -> str:
        """Return a debugging representation."""
        return "SystemClock()"


class FixedClock:
    """Deterministic clock for tests. Starts at a fixed instant and moves only when told.

    ``advance`` rather than a mutable ``set`` is the primary API so that a test cannot
    accidentally move time backwards, which would invalidate lease logic that assumes
    monotonicity.
    """

    __slots__ = ("_now",)

    def __init__(self, start: dt.datetime | None = None) -> None:
        """Initialise the clock.

        Args:
            start: Initial instant. Defaults to 2026-01-01T00:00:00Z. A naive datetime is
                rejected rather than silently assumed to be UTC.

        """
        if start is None:
            start = dt.datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            msg = "FixedClock requires a timezone-aware datetime; naive datetimes are ambiguous"
            raise ValueError(msg)
        self._now = start.astimezone(UTC)

    def now(self) -> dt.datetime:
        """Return the current fixed instant."""
        return self._now

    def advance(self, seconds: float) -> dt.datetime:
        """Move the clock forward by ``seconds`` and return the new instant.

        Args:
            seconds: Non-negative duration to advance by.

        Returns:
            The new current time.

        Raises:
            ValueError: If ``seconds`` is negative.

        """
        if seconds < 0:
            msg = "FixedClock cannot move backwards"
            raise ValueError(msg)
        self._now = self._now + dt.timedelta(seconds=seconds)
        return self._now

    def __repr__(self) -> str:
        """Return a debugging representation including the current instant."""
        return f"FixedClock({self._now.isoformat()})"
