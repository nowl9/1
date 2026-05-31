"""Feed health tracker — records last-tick timestamps per data source.

Used by the signal filter's Data Freshness Gate to reject signals when any
upstream feed has gone stale.  Updated by feed tasks in main.py; read by
the filter via the ``ctx["feed_health"]`` key.

Design
------
* Single shared instance per agent, updated from the event loop.
* No locks needed (single event loop).
* ``staleness_ms(source)`` returns milliseconds since last tick, or inf if
  the feed has never received a tick.

Simulated-clock seam (build step 1, Fork 3)
-------------------------------------------
``staleness_ms`` reads "now" through an injectable
``clock: Callable[[], datetime]`` instead of calling
``datetime.now(timezone.utc)`` inline.  In live mode the clock defaults to
wall-clock (behaviour unchanged); under replay the agent injects a
:class:`btc_pm_arb.clock.SimulatedClock` so staleness is measured against
recorded sim-time, not wall-clock — see clock.py for why an
as-fast-as-possible replay needs this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import structlog

from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class FeedHealthTracker:
    """Track the recency of the last tick received from each data source.

    Usage::

        health = FeedHealthTracker()
        health.record_tick(DataSource.DERIBIT)   # called on each tick
        ms = health.staleness_ms(DataSource.DERIBIT)

    ``clock`` defaults to wall-clock; the agent injects a
    :class:`btc_pm_arb.clock.SimulatedClock` in replay mode so freshness is
    measured against sim-time.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._last_tick: dict[DataSource, datetime] = {}
        self._clock = clock or _default_clock

    def record_tick(
        self,
        source: DataSource,
        ts: datetime | None = None,
    ) -> None:
        """Mark the current time as the last successful tick from ``source``."""
        self._last_tick[source] = ts or self._clock()

    def staleness_ms(self, source: DataSource) -> float:
        """Milliseconds since the last tick from ``source``.

        Returns float('inf') if no tick has been received yet.  "Now" is
        read through the injected clock (wall-clock by default; sim-time
        under replay).
        """
        last = self._last_tick.get(source)
        if last is None:
            return float("inf")
        return (self._clock() - last).total_seconds() * 1000.0

    def staleness_s(self, source: DataSource) -> float:
        """Seconds since the last tick from ``source``."""
        return self.staleness_ms(source) / 1000.0

    def is_stale(self, source: DataSource, max_age_s: float) -> bool:
        return self.staleness_s(source) > max_age_s

    def all_staleness_ms(self) -> dict[str, float]:
        """Return staleness for all feeds as a JSON-serializable dict.

        Keys are the lowercase enum *values* (``"deribit"``, ``"polymarket"``,
        ``"kalshi"``) — NOT ``str(member)``, which on a ``(str, Enum)`` mixin
        produces the verbose ``"DataSource.DERIBIT"`` form on Python 3.11+ and
        causes the dashboard's ``feeds["deribit"]`` lookup to miss.
        """
        return {src.value: self.staleness_ms(src) for src in DataSource}

    def summary(self) -> dict[str, float]:
        return self.all_staleness_ms()
