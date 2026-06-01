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
* O(amortized 1) per-update and per-rv() call.  For each configured window we
  maintain incremental aggregates (count, Σ returns, Σ returns², Σ intervals)
  and a FIFO of (t_earlier, log_return, interval) entries.  Eviction (time-
  based + capacity-bound safety) subtracts each entry's contribution when it
  ages out.  This replaces an earlier O(N)-per-update implementation that
  saturated the asyncio event loop after ~90 s of operation; see diagnostic
  rounds 4–5.
* The legacy ``_data`` deque of (timestamp, log_price) is retained for
  backward compatibility with the public ``n_points`` / ``oldest_ts`` /
  ``newest_ts`` properties (and main.py's read of ``_data[-1][1]`` for
  ``btc_price`` rendering).  It does not power ``rv()`` any more.
* Thread-safe enough for single-event-loop use (no actual locks needed).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Sequence

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


# ── Per-window incremental aggregates ─────────────────────────────────────────

@dataclass
class _WindowAggregates:
    """Running aggregates for one realized-vol window — supports O(1)
    update and O(1) rv computation via incremental statistics.

    Each entry represents a return r_i = log_price_i − log_price_{i−1}
    between two consecutive observed prices.  The entry's stored timestamp
    is the *earlier* endpoint t_{i−1}; eviction criterion is
    ``t_earlier < cutoff``, matching the legacy ``ts >= cutoff`` filter
    (a return is in window iff both endpoints are; equivalently iff
    t_{i−1} ≥ cutoff because t_i > t_{i−1}).
    """

    window_h: float
    # FIFO of (t_earlier, log_return, interval_seconds) — one per return.
    entries: deque[tuple[datetime, float, float]] = field(default_factory=deque)
    sum_returns: float = 0.0
    sum_returns_sq: float = 0.0
    sum_intervals: float = 0.0


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
    # Injectable "now" source (sim-clock seam, Fork 3 — sibling of the
    # vol_surface.update(now=) fix).  ``maybe_update``'s throttle/timestamp
    # and ``rv``'s query-time eviction read this instead of calling
    # ``datetime.now(timezone.utc)`` inline, so an as-fast-as-possible replay
    # samples + annualizes off SIM time (true recorded spacing) rather than
    # wall-clock (which collapses every interval to ~milliseconds and reads a
    # phantom HIGH regime).  None -> wall-clock, so a default tracker and every
    # existing caller/test are byte-for-byte unchanged; main.py injects
    # ``Agent.clock`` (live clock.now() == datetime.now(utc), so live is also
    # unchanged).  Mirrors FeedHealthTracker(clock=...).
    clock: Callable[[], datetime] | None = None

    # (timestamp, log_price) pairs in chronological order — retained for
    # backward compatibility with n_points / oldest_ts / newest_ts properties
    # and main.py's _data[-1][1] read.  Not used by rv() any more.
    _data: deque[tuple[datetime, float]] = field(
        default_factory=lambda: deque(maxlen=100_000), init=False
    )
    _prev_regime: VolRegime | None = field(default=None, init=False)

    # Most recent (timestamp, log_price), used to compute the next return.
    _previous: tuple[datetime, float] | None = field(default=None, init=False)
    # Per-window incremental aggregates, keyed by window_h.
    _windows: dict[float, _WindowAggregates] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        # Ensure max_points matches deque maxlen
        self._data = deque(maxlen=self.max_points)
        if 1.0 not in self.windows_h:
            self.windows_h = [1.0] + self.windows_h
        # Pre-build per-window aggregate state for every configured window.
        # Calls to rv(window_h) for unconfigured windows return None.
        self._windows = {w: _WindowAggregates(window_h=w) for w in self.windows_h}
        logger.info(
            "rv_tracker.initialized",
            windows_h=list(self.windows_h),
            max_points=self.max_points,
            algorithm="incremental_aggregates_o1",
        )

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def update(self, price: float, ts: datetime | None = None) -> None:
        """Add a new price observation.

        Args:
            price: BTC index price in USD (must be > 0).
            ts:    Timestamp; defaults to ``datetime.now(timezone.utc)``.

        O(amortized 1).  Updates the legacy ``_data`` deque (for the public
        ``n_points`` / ``oldest_ts`` / ``newest_ts`` properties) and the
        per-window incremental aggregates that drive :meth:`rv`.
        Out-of-order or duplicate-timestamp updates are rejected — production
        callers feed strictly monotonic timestamps via :meth:`maybe_update`.
        """
        if price <= 0:
            return
        now = ts or self._now()
        log_price = math.log(price)

        # Reject non-monotonic timestamps; would otherwise produce zero or
        # negative intervals and break the rv arithmetic.
        if self._previous is not None:
            prev_ts, _ = self._previous
            if now <= prev_ts:
                return

        # Backward-compatible: append to legacy (ts, log_price) deque.
        self._data.append((now, log_price))

        # Per-window incremental aggregates.  No-op on the very first
        # observation (no previous price to diff against).
        if self._previous is not None:
            prev_ts, prev_lp = self._previous
            log_return = log_price - prev_lp
            interval_s = (now - prev_ts).total_seconds()
            for w in self._windows.values():
                self._evict_stale(w, now)
                cutoff = now - timedelta(hours=w.window_h)
                if prev_ts < cutoff:
                    # New return is already past this window's horizon;
                    # don't add it.  The previous-price endpoint aged out
                    # before this update arrived.
                    continue
                # Capacity safety: time-based eviction is the primary
                # mechanism, but at very high update rates against very
                # long windows the deque could grow unboundedly otherwise.
                if len(w.entries) >= self.max_points:
                    ev_t, ev_ret, ev_int = w.entries.popleft()
                    w.sum_returns -= ev_ret
                    w.sum_returns_sq -= ev_ret * ev_ret
                    w.sum_intervals -= ev_int
                w.entries.append((prev_ts, log_return, interval_s))
                w.sum_returns += log_return
                w.sum_returns_sq += log_return * log_return
                w.sum_intervals += interval_s

        self._previous = (now, log_price)
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
        now = self._now()
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

        Returns None if fewer than 2 returns fall within the window, or if
        ``window_h`` was not pre-configured at construction time.

        Formula (equivalent to the legacy std×sqrt(periods_per_year) form,
        derived from incremental aggregates):
            mean = Σreturns / n
            var  = (Σreturns² − n·mean²) / (n − 1)         # sample, ddof=1
            mean_interval_s = Σintervals / n
            σ_rv = sqrt(var) × sqrt(SECONDS_PER_YEAR / mean_interval_s)

        O(amortized 1): five floating-point ops plus the eviction pass at
        the front of the window deque (each entry evicted at most once
        across its lifetime).
        """
        w = self._windows.get(window_h)
        if w is None:
            # Unconfigured window — production callers (main.py:rv(1.0),
            # rv(24.0); current_regime) all use windows_h members.  Add to
            # constructor's windows_h for new windows; on-demand fallback
            # is intentionally not provided so all rv() calls stay O(1).
            return None

        # Eviction at query time is required for correctness when wall-time
        # has elapsed since the last update (e.g., test_rv_returns_none_for_
        # empty_window feeds prices then asserts rv() at a later instant
        # without further updates).  Each entry is evicted at most once
        # over its lifetime, keeping the per-call amortized cost O(1).
        self._evict_stale(w, self._now())

        n = len(w.entries)
        if n < 2:
            return None

        mean_return = w.sum_returns / n
        var = (w.sum_returns_sq - n * mean_return * mean_return) / (n - 1)
        if var < 0:
            # Floating-point cancellation noise when all returns are
            # near-identical; true variance is non-negative.  Clamp.
            var = 0.0
        std = math.sqrt(var)

        mean_interval_s = w.sum_intervals / n
        if mean_interval_s <= 0:
            return None

        periods_per_year = _SECONDS_PER_YEAR / mean_interval_s
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

    def _now(self) -> datetime:
        """Current UTC time via the injected clock seam, else wall-clock.

        ``clock`` is a ``Callable[[], datetime]`` (typically a
        :class:`~btc_pm_arb.clock.SimulatedClock`).  None -> wall-clock so a
        default tracker is unchanged; under replay the injected sim-clock makes
        the throttle cadence, stored timestamps, and query-time eviction all
        read the recorded timeline.
        """
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc)

    def _evict_stale(self, w: _WindowAggregates, now: datetime) -> None:
        """Pop aged-out entries from the front of ``w.entries`` and subtract
        each evicted entry's contribution from the running aggregates.

        Eviction criterion: ``t_earlier < now − window_h``.  Strict ``<``
        matches the legacy ``ts >= cutoff`` filter (cutoff-equal entries
        stay in the window).
        """
        cutoff = now - timedelta(hours=w.window_h)
        while w.entries:
            head_t, head_ret, head_int = w.entries[0]
            if head_t < cutoff:
                w.entries.popleft()
                w.sum_returns -= head_ret
                w.sum_returns_sq -= head_ret * head_ret
                w.sum_intervals -= head_int
            else:
                break

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
