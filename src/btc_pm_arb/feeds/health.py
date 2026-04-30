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
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)


class FeedHealthTracker:
    """Track the recency of the last tick received from each data source.

    Usage::

        health = FeedHealthTracker()
        health.record_tick(DataSource.DERIBIT)   # called on each tick
        ms = health.staleness_ms(DataSource.DERIBIT)
    """

    def __init__(self) -> None:
        self._last_tick: dict[DataSource, datetime] = {}

    def record_tick(
        self,
        source: DataSource,
        ts: datetime | None = None,
    ) -> None:
        """Mark the current time as the last successful tick from ``source``."""
        self._last_tick[source] = ts or datetime.now(timezone.utc)

    def staleness_ms(self, source: DataSource) -> float:
        """Milliseconds since the last tick from ``source``.

        Returns float('inf') if no tick has been received yet.
        """
        last = self._last_tick.get(source)
        if last is None:
            return float("inf")
        return (datetime.now(timezone.utc) - last).total_seconds() * 1000.0

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
