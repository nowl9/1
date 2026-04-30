"""In-memory probability grid keyed by (strike, expiry).

Provides:
  - O(1) exact lookup via dict
  - O(log n) interpolated lookup for arbitrary (K, T) pairs via bisect +
    linear interpolation in strike space and total-variance time space

The cache is the integration point between the pricing engine and the
signal generator: the digital pricer writes probability quotes here after
(optionally) applying the basis adjustment, and the signal generator reads
from it when comparing against prediction market prices.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from btc_pm_arb.models import DataSource, ProbabilityQuote

_SECONDS_PER_YEAR = 365.25 * 86400.0


# ── Cache entry ───────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    """Probability snapshot at one (strike, expiry) grid point."""

    strike: float
    expiry: datetime
    bid_prob: float   # lower probability bound
    ask_prob: float   # upper probability bound
    mid_prob: float   # midpoint probability
    source: DataSource
    timestamp: datetime

    @property
    def spread(self) -> float:
        return self.ask_prob - self.bid_prob


# ── Cache ─────────────────────────────────────────────────────────────────────

class ProbabilityCache:
    """In-memory probability grid with O(1) exact lookup and interpolation.

    Grid structure: (strike, expiry) → CacheEntry

    Interpolation:
      - Across strikes: linear in probability space (for a fixed expiry).
      - Across expiries: linear in calendar time (simple for MVP; total-var
        interpolation is handled upstream in VolSurface).

    Usage::

        cache = ProbabilityCache()
        cache.update(strike=60_000, expiry=dt, bid=0.44, ask=0.56, mid=0.50,
                     source=DataSource.DERIBIT)
        entry = cache.get(60_000, dt)          # exact O(1)
        entry = cache.interpolate(61_000, dt)  # interpolated
    """

    def __init__(self) -> None:
        self._grid: dict[tuple[float, datetime], CacheEntry] = {}
        # Sorted lists for bisect-based interpolation
        self._expiries: list[datetime] = []
        self._strikes_by_expiry: dict[datetime, list[float]] = {}

    # ── writes ────────────────────────────────────────────────────────────

    def update(
        self,
        strike: float,
        expiry: datetime,
        bid_prob: float,
        ask_prob: float,
        mid_prob: float,
        source: DataSource,
        timestamp: datetime | None = None,
    ) -> None:
        """Insert or overwrite a single grid point."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        entry = CacheEntry(
            strike=strike,
            expiry=expiry,
            bid_prob=float(np.clip(bid_prob, 0.0, 1.0)),
            ask_prob=float(np.clip(ask_prob, 0.0, 1.0)),
            mid_prob=float(np.clip(mid_prob, 0.0, 1.0)),
            source=source,
            timestamp=timestamp,
        )
        key = (strike, expiry)
        is_new = key not in self._grid
        self._grid[key] = entry

        if is_new:
            if expiry not in self._strikes_by_expiry:
                self._strikes_by_expiry[expiry] = []
                bisect.insort(self._expiries, expiry)
            strikes = self._strikes_by_expiry[expiry]
            if strike not in strikes:
                bisect.insort(strikes, strike)

    def update_from_quote(self, quote: ProbabilityQuote) -> None:
        """Convenience wrapper: update from a ProbabilityQuote model."""
        self.update(
            strike=quote.strike,
            expiry=quote.expiry,
            bid_prob=quote.bid_prob,
            ask_prob=quote.ask_prob,
            mid_prob=quote.mid_prob,
            source=quote.source,
            timestamp=quote.timestamp,
        )

    # ── reads ─────────────────────────────────────────────────────────────

    def get(self, strike: float, expiry: datetime) -> CacheEntry | None:
        """O(1) exact lookup.  Returns None on miss."""
        return self._grid.get((strike, expiry))

    def interpolate(
        self,
        strike: float,
        expiry: datetime,
        interp_expiries: bool = True,
    ) -> CacheEntry | None:
        """Return a (possibly interpolated) entry for arbitrary (strike, expiry).

        Algorithm:
          1. Exact hit → return immediately.
          2. Find surrounding expiries lo/hi.
          3. Interpolate across strikes at each expiry.
          4. Interpolate between the two expiry-slices in calendar-time.

        Returns None if there is insufficient grid data.
        """
        exact = self.get(strike, expiry)
        if exact is not None:
            return exact

        if not self._expiries:
            return None

        if not interp_expiries:
            nearest = self._nearest_expiry(expiry)
            return self._interp_strike(strike, nearest) if nearest else None

        lo_exp, hi_exp = self._surrounding_expiries(expiry)

        if lo_exp is None and hi_exp is None:
            return None
        if lo_exp == hi_exp or hi_exp is None:
            return self._interp_strike(strike, lo_exp or hi_exp)
        if lo_exp is None:
            return self._interp_strike(strike, hi_exp)

        e_lo = self._interp_strike(strike, lo_exp)
        e_hi = self._interp_strike(strike, hi_exp)

        if e_lo is None:
            return e_hi
        if e_hi is None:
            return e_lo

        return self._interp_expiry(strike, expiry, e_lo, lo_exp, e_hi, hi_exp)

    def entries_for_expiry(self, expiry: datetime) -> list[tuple[float, CacheEntry]]:
        """All (strike, entry) pairs for a given expiry, sorted by strike."""
        result: list[tuple[float, CacheEntry]] = []
        for s in self._strikes_by_expiry.get(expiry, []):
            e = self._grid.get((s, expiry))
            if e is not None:
                result.append((s, e))
        return result

    def all_expiries(self) -> list[datetime]:
        """All expiry datetimes in the cache, sorted ascending."""
        return list(self._expiries)

    def all_strikes_for_expiry(self, expiry: datetime) -> list[float]:
        return list(self._strikes_by_expiry.get(expiry, []))

    def __len__(self) -> int:
        return len(self._grid)

    def clear(self) -> None:
        self._grid.clear()
        self._expiries.clear()
        self._strikes_by_expiry.clear()

    # ── private ───────────────────────────────────────────────────────────

    def _nearest_expiry(self, expiry: datetime) -> datetime | None:
        if not self._expiries:
            return None
        idx = bisect.bisect_left(self._expiries, expiry)
        if idx == 0:
            return self._expiries[0]
        if idx >= len(self._expiries):
            return self._expiries[-1]
        before = self._expiries[idx - 1]
        after = self._expiries[idx]
        d_before = abs((before - expiry).total_seconds())
        d_after = abs((after - expiry).total_seconds())
        return before if d_before <= d_after else after

    def _surrounding_expiries(
        self, expiry: datetime
    ) -> tuple[datetime | None, datetime | None]:
        if not self._expiries:
            return None, None
        idx = bisect.bisect_left(self._expiries, expiry)
        lo = self._expiries[idx - 1] if idx > 0 else None
        hi = self._expiries[idx] if idx < len(self._expiries) else None
        return lo, hi

    def _interp_strike(
        self, strike: float, expiry: datetime | None
    ) -> CacheEntry | None:
        """Linear interpolation across strikes for a fixed expiry."""
        if expiry is None:
            return None
        strikes = self._strikes_by_expiry.get(expiry)
        if not strikes:
            return None

        idx = bisect.bisect_left(strikes, strike)

        if idx == 0:
            return self._grid.get((strikes[0], expiry))
        if idx >= len(strikes):
            return self._grid.get((strikes[-1], expiry))
        if strikes[idx] == strike:
            return self._grid.get((strike, expiry))

        lo_s, hi_s = strikes[idx - 1], strikes[idx]
        e_lo = self._grid.get((lo_s, expiry))
        e_hi = self._grid.get((hi_s, expiry))

        if e_lo is None:
            return e_hi
        if e_hi is None:
            return e_lo

        frac = (strike - lo_s) / max(hi_s - lo_s, 1.0)

        def lerp(a: float, b: float) -> float:
            return a + (b - a) * frac

        return CacheEntry(
            strike=strike,
            expiry=expiry,
            bid_prob=float(np.clip(lerp(e_lo.bid_prob, e_hi.bid_prob), 0.0, 1.0)),
            ask_prob=float(np.clip(lerp(e_lo.ask_prob, e_hi.ask_prob), 0.0, 1.0)),
            mid_prob=float(np.clip(lerp(e_lo.mid_prob, e_hi.mid_prob), 0.0, 1.0)),
            source=e_lo.source,
            timestamp=max(e_lo.timestamp, e_hi.timestamp),
        )

    def _interp_expiry(
        self,
        strike: float,
        target_expiry: datetime,
        e_lo: CacheEntry,
        lo_exp: datetime,
        e_hi: CacheEntry,
        hi_exp: datetime,
    ) -> CacheEntry:
        """Linear interpolation in calendar-time between two expiry slices."""
        now = datetime.now(timezone.utc)

        def years(dt: datetime) -> float:
            return max((dt - now).total_seconds() / _SECONDS_PER_YEAR, 0.0)

        T_lo, T_hi, T_q = years(lo_exp), years(hi_exp), years(target_expiry)

        if T_hi <= T_lo or not (T_lo <= T_q <= T_hi):
            return e_lo if abs(T_q - T_lo) <= abs(T_q - T_hi) else e_hi

        frac = (T_q - T_lo) / (T_hi - T_lo)

        def lerp(a: float, b: float) -> float:
            return a + (b - a) * frac

        return CacheEntry(
            strike=strike,
            expiry=target_expiry,
            bid_prob=float(np.clip(lerp(e_lo.bid_prob, e_hi.bid_prob), 0.0, 1.0)),
            ask_prob=float(np.clip(lerp(e_lo.ask_prob, e_hi.ask_prob), 0.0, 1.0)),
            mid_prob=float(np.clip(lerp(e_lo.mid_prob, e_hi.mid_prob), 0.0, 1.0)),
            source=e_lo.source,
            timestamp=max(e_lo.timestamp, e_hi.timestamp),
        )
