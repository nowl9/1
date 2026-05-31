"""Simulated clock seam -- the single injectable time source for replay.

Build step 1 (paper-trading plan section 3.1, Fork 3).  Today the
freshness-sensitive code paths call ``datetime.now(timezone.utc)`` inline:
``filters._reject_stale_data`` / ``_reject_expiry_bounds`` and
``FeedHealthTracker.staleness_ms``.  Under an as-fast-as-possible replay
(Fork 3, plan section 4.3) those wall-clock reads make every recorded tick
look minutes/hours stale within milliseconds, so the gates reject
everything and the pipeline produces zero signals.

:class:`SimulatedClock` is the seam that breaks that coupling.  In ``live``
mode it delegates to ``datetime.now(timezone.utc)`` (default-live behaviour
is byte-for-byte unchanged).  In ``replay`` mode it returns an *advanceable*
timestamp that a replay reader (build step 5 -- a SEPARATE follow-up; NOT
built here) drives off the recorded ``ts`` stream via :meth:`advance_to`.

The precedent is :class:`KalshiSettlementPoller`, which already takes an
injectable ``clock: Callable[[], datetime]`` (paper_settlement.py).  This
object is itself ``Callable[[], datetime]`` (see :meth:`__call__`) so it is
a drop-in for that parameter and for any other call site that previously
read the wall clock.

Determinism note
----------------
``Date.now``-style reads are the enemy of a reproducible replay.  By
funnelling every freshness read through one object, build step 5 can make a
replay run produce an identical ledger to a prior run: same recorded ``ts``
stream in, same :meth:`advance_to` calls, same gate decisions out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

ClockMode = Literal["live", "replay"]


class SimulatedClock:
    """Injectable UTC clock with a live (wall-clock) and a replay mode.

    Usage::

        clock = SimulatedClock("live")          # delegates to now(utc)
        clock.now()                              # -> datetime

        clock = SimulatedClock("replay", start=t0)
        clock.now()                              # -> t0
        clock.advance_to(t1)                     # reader drives this
        clock.now()                              # -> t1

    The object is also callable (``clock()`` == ``clock.now()``) so it
    satisfies the ``Callable[[], datetime]`` contract the existing
    :class:`KalshiSettlementPoller.clock` parameter expects.
    """

    def __init__(
        self, mode: ClockMode = "live", *, start: datetime | None = None
    ) -> None:
        if mode not in ("live", "replay"):
            raise ValueError(f"mode must be 'live' or 'replay', got {mode!r}")
        self._mode: ClockMode = mode
        # Current replay position.  None until the first advance_to (or the
        # ``start`` anchor) is set.  Unused in live mode.
        self._current: datetime | None = _ensure_utc(start) if start is not None else None

    @property
    def mode(self) -> ClockMode:
        return self._mode

    def now(self) -> datetime:
        """Return the current UTC time under the active mode.

        Live: ``datetime.now(timezone.utc)``.  Replay: the last value set by
        :meth:`advance_to` (or the ``start`` anchor).  Raises if a replay
        clock is read before it has been positioned -- a louder failure than
        silently returning wall-clock, which would defeat the seam.
        """
        if self._mode == "live":
            return datetime.now(timezone.utc)
        if self._current is None:
            raise RuntimeError(
                "replay clock read before advance_to/start -- the replay "
                "reader (build step 5) must position the clock first"
            )
        return self._current

    def advance_to(self, ts: datetime) -> None:
        """Move the replay clock to ``ts`` (must be monotonic non-decreasing).

        Only valid in replay mode; the recorded ``ts`` stream is monotonic
        per source, so a backwards move signals a reader bug and raises.
        """
        if self._mode != "replay":
            raise RuntimeError("advance_to is only valid in replay mode")
        ts = _ensure_utc(ts)
        if self._current is not None and ts < self._current:
            raise ValueError(
                f"replay clock cannot move backwards: {ts.isoformat()} "
                f"< {self._current.isoformat()}"
            )
        self._current = ts

    def __call__(self) -> datetime:
        return self.now()


def _ensure_utc(ts: datetime) -> datetime:
    """Coerce a naive datetime to UTC; pass through tz-aware unchanged."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts
