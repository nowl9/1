"""Realized volatility tracker and volatility regime detector.

Computes rolling realized volatility from a stream of BTC index prices using
log-return standard deviation, annualized.  Supports multiple simultaneous
windows (default 1 h, 4 h, 24 h).

Volatility regimes
------------------
  LOW    rv_1h < 30 % annualized   → tighter edge threshold is acceptable
  NORMAL 30 % ≤ rv_1h < 80 %      → use base thresholds
  HIGH   rv_1h ≥ 80 %             → require wider edge; settlement basis risk elevated

Design
------
* Pure data structure — no I/O, no async.  Feed prices in from any async task
  via ``update(price, ts)``.
* Uses a deque of (timestamp, log_price) to keep only the data needed for the
  longest configured window.
* Thread-safe enough for single-event-loop use (no actual locks needed).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Sequence

import numpy as np
import structlog

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# ── Regimes ────────────────────────────────────────────────────────────────────

class VolRegime(str, Enum):
    LOW = "low"        # rv_1h < 30 %
    NORMAL = "normal"  # 30 % ≤ rv_1h < 80 %
    HIGH = "high"      # rv_1h ≥ 80 %

# Regime thresholds (annualized rv as decimal)
_LOW_THRESHOLD: float = 0.30
_HIGH_THRESHOLD: float = 0.80

# Edge multipliers per regime
REGIME_EDGE_MULTIPLIER: dict[VolRegime, float] = {
    VolRegime.LOW:    0.8,
    VolRegime.NORMAL: 1.0,
    VolRegime.HIGH:   1.5,
}

_SECONDS_PER_YEAR: float = 365.25 * 86_400.0


# ── Tracker ────────────────────────────────────────────────────────────────────

@dataclass
class RealizedVolTracker:
    """Maintains a rolling window of BTC index prices and computes realized vol.

    Args:
        windows_h:    List of window sizes in hours (must include 1.0 for regime).
        max_points:   Hard cap on stored data points (memory safety).

    Usage::

        rv_tracker = RealizedVolTracker()
        rv_tracker.update(price=62_000.0)
        regime = rv_tracker.current_regime()
        rv_1h = rv_tracker.rv(window_h=1.0)
    """

    windows_h: list[float] = field(default_factory=lambda: [1.0, 4.0, 24.0])
    max_points: int = 100_000

    # (timestamp, log_price) pairs in chronological order
    _data: deque[tuple[datetime, float]] = field(
        default_factory=lambda: deque(maxlen=100_000), init=False
    )
    _prev_regime: VolRegime | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Ensure max_points matches deque maxlen
        self._data = deque(maxlen=self.max_points)
        if 1.0 not in self.windows_h:
            self.windows_h = [1.0] + self.windows_h

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def update(self, price: float, ts: datetime | None = None) -> None:
        """Add a new price observation.

        Args:
            price: BTC index price in USD (must be > 0).
            ts:    Timestamp; defaults to ``datetime.now(timezone.utc)``.
        """
        if price <= 0:
            return
        now = ts or datetime.now(timezone.utc)
        self._data.append((now, math.log(price)))
        self._check_regime_change()

    def maybe_update(
        self,
        price: float,
        min_interval_s: float = 1.0,
    ) -> bool:
        """Throttled variant of :meth:`update`.

        Applies the update only if at least ``min_interval_s`` seconds of
        wall-time have elapsed since the most recent observation; returns
        ``True`` when an update was applied, ``False`` when throttled.

        Motivation: the realized-vol calculation cares about the BTC
        *index-price* time series (which natively updates at most a few
        Hz), but the upstream caller (``Agent.ingest_tick``) sees one
        ``index_price`` value per *option-tick* event — hundreds per
        second across 912 instruments, almost all carrying the same
        index price.  Calling the O(N) :meth:`update` per option tick
        saturates the event loop within ~90 s of operation; throttling
        to 1 Hz reduces the call rate by 2-3 orders of magnitude with no
        material loss of fidelity for vol estimation.

        ``min_interval_s`` defaults to 1.0 s, suitable for the BTC index
        feed; callers with different cadence requirements can override.
        """
        if price <= 0:
            return False
        now = datetime.now(timezone.utc)
        last = self.newest_ts
        if last is not None:
            elapsed_s = (now - last).total_seconds()
            if elapsed_s < min_interval_s:
                return False
        self.update(price, ts=now)
        return True

    # ── Queries ───────────────────────────────────────────────────────────────

    def rv(self, window_h: float) -> float | None:
        """Annualized realized vol for the trailing ``window_h`` hours.

        Returns None if fewer than 2 observations fall within the window.

        Formula: ``σ_rv = std(log_returns) × sqrt(N_per_year)``
        where N_per_year is the annualized number of return intervals.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
        pts = [(ts, lp) for ts, lp in self._data if ts >= cutoff]
        if len(pts) < 2:
            return None

        log_prices = np.array([lp for _, lp in pts])
        log_returns = np.diff(log_prices)
        if len(log_returns) == 0:
            return None

        # Average interval between observations in seconds
        times = [ts for ts, _ in pts]
        intervals_s = np.array([
            (times[i + 1] - times[i]).total_seconds()
            for i in range(len(times) - 1)
        ])
        mean_interval_s = float(np.mean(intervals_s))
        if mean_interval_s <= 0:
            return None

        periods_per_year = _SECONDS_PER_YEAR / mean_interval_s
        std = float(np.std(log_returns, ddof=1))
        return std * math.sqrt(periods_per_year)

    def rv_all(self) -> dict[float, float | None]:
        """Compute rv for all configured windows at once."""
        return {w: self.rv(w) for w in self.windows_h}

    def current_regime(self) -> VolRegime:
        """Return current volatility regime based on 1-hour realized vol."""
        rv_1h = self.rv(1.0)
        if rv_1h is None:
            return VolRegime.NORMAL   # neutral default when insufficient data
        if rv_1h < _LOW_THRESHOLD:
            return VolRegime.LOW
        if rv_1h < _HIGH_THRESHOLD:
            return VolRegime.NORMAL
        return VolRegime.HIGH

    def effective_min_edge(self, base_min_edge: float) -> float:
        """Return regime-adjusted minimum edge threshold."""
        return base_min_edge * REGIME_EDGE_MULTIPLIER[self.current_regime()]

    @property
    def n_points(self) -> int:
        return len(self._data)

    @property
    def oldest_ts(self) -> datetime | None:
        return self._data[0][0] if self._data else None

    @property
    def newest_ts(self) -> datetime | None:
        return self._data[-1][0] if self._data else None

    # ── Private ───────────────────────────────────────────────────────────────

    def _check_regime_change(self) -> None:
        regime = self.current_regime()
        if regime != self._prev_regime:
            if self._prev_regime is not None:
                logger.info(
                    "vol_regime.changed",
                    from_regime=self._prev_regime,
                    to_regime=regime,
                    rv_1h=round(self.rv(1.0) or 0, 4),
                )
            self._prev_regime = regime
