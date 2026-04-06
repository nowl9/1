"""Contract matcher — links prediction-market contracts to options probability cache entries.

The matcher resolves the fundamental problem: prediction market contracts use
human-readable titles ("BTC above $100k by June 30") while the options cache
is keyed by exact (strike, expiry) grid points.

Matching pipeline:
  1. Validate PM contract has a parseable strike and expiry (from normalizer).
  2. Find the nearest cache expiry within max_expiry_gap_hours (default 24 h).
  3. Find the nearest cached strike within max_strike_gap_pct (default 2 %).
  4. Use the cache's interpolation for in-between strikes.
  5. Score the match quality on [0, 1] using a linear penalty formula.

A MatchResult with quality < 0 or outside the gap tolerances is rejected
before any edge calculation is attempted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from btc_pm_arb.models import DataSource, PredictionMarketTick, ProbabilityQuote
from btc_pm_arb.pricing.cache import CacheEntry, ProbabilityCache

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# Default tolerance thresholds
DEFAULT_MAX_STRIKE_GAP_PCT: float = 0.02    # 2 % relative strike distance
DEFAULT_MAX_EXPIRY_GAP_HOURS: float = 24.0  # 24 hours


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """Output of a successful contract match."""

    pm_tick: PredictionMarketTick      # raw PM contract data
    pm_quote: ProbabilityQuote         # PM prices as a probability quote
    options_entry: CacheEntry          # options-derived probability at matched (K, T)

    matched_strike: float              # cache strike used (may ≠ pm_tick.strike)
    matched_expiry: datetime           # cache expiry used (may ≠ pm_tick.expiry)

    strike_gap_pct: float              # |pm_K - cache_K| / pm_K  in [0, max]
    expiry_gap_hours: float            # |pm_T - cache_T| in hours, in [0, max]
    match_quality: float               # 0 (barely acceptable) → 1 (perfect)
    is_interpolated: bool              # True when cache returned an interpolated entry

    @property
    def is_acceptable(self) -> bool:
        return self.match_quality > 0 and self.strike_gap_pct <= DEFAULT_MAX_STRIKE_GAP_PCT


# ── Matcher ───────────────────────────────────────────────────────────────────

class ContractMatcher:
    """Match prediction-market contracts to entries in the ProbabilityCache.

    Usage::

        matcher = ContractMatcher()
        result = matcher.match(pm_tick, cache)
        if result is not None:
            ...  # proceed to edge calculation
    """

    def __init__(
        self,
        max_strike_gap_pct: float = DEFAULT_MAX_STRIKE_GAP_PCT,
        max_expiry_gap_hours: float = DEFAULT_MAX_EXPIRY_GAP_HOURS,
    ) -> None:
        self.max_strike_gap_pct = max_strike_gap_pct
        self.max_expiry_gap_hours = max_expiry_gap_hours

    def match(
        self,
        pm_tick: PredictionMarketTick,
        cache: ProbabilityCache,
    ) -> MatchResult | None:
        """Find the best matching cache entry for a PM contract.

        Returns ``None`` if:
        - The PM contract has no parseable strike or expiry.
        - No cache expiry lies within ``max_expiry_gap_hours``.
        - The nearest cached strike at that expiry exceeds ``max_strike_gap_pct``.
        - The PM contract has no bid/ask prices.
        """
        # ── validate inputs ───────────────────────────────────────────────

        if pm_tick.strike is None or pm_tick.expiry is None:
            logger.debug("matcher.no_strike_expiry", contract=pm_tick.contract_id)
            return None

        if pm_tick.yes_bid is None or pm_tick.yes_ask is None:
            logger.debug("matcher.no_pm_prices", contract=pm_tick.contract_id)
            return None

        pm_quote = _pm_tick_to_quote(pm_tick)
        if pm_quote is None:
            return None

        all_expiries = cache.all_expiries()
        if not all_expiries:
            return None

        # ── step 1: find nearest cache expiry within tolerance ─────────────

        candidates = [
            (abs((pm_tick.expiry - e).total_seconds()) / 3600.0, e)
            for e in all_expiries
            if abs((pm_tick.expiry - e).total_seconds()) / 3600.0
            <= self.max_expiry_gap_hours
        ]
        if not candidates:
            logger.debug(
                "matcher.no_expiry_within_gap",
                contract=pm_tick.contract_id,
                pm_expiry=pm_tick.expiry.isoformat(),
                max_gap_h=self.max_expiry_gap_hours,
            )
            return None

        expiry_gap_h, best_expiry = min(candidates, key=lambda x: x[0])

        # ── step 2: find nearest cache strike within tolerance ─────────────

        strikes = cache.all_strikes_for_expiry(best_expiry)
        if not strikes:
            return None

        nearest_strike = min(strikes, key=lambda s: abs(s - pm_tick.strike))
        strike_gap_pct = abs(pm_tick.strike - nearest_strike) / pm_tick.strike

        if strike_gap_pct > self.max_strike_gap_pct:
            logger.debug(
                "matcher.strike_gap_too_large",
                contract=pm_tick.contract_id,
                pm_strike=pm_tick.strike,
                nearest_strike=nearest_strike,
                gap_pct=round(strike_gap_pct * 100, 2),
                max_pct=round(self.max_strike_gap_pct * 100, 2),
            )
            return None

        # ── step 3: get (possibly interpolated) cache entry ────────────────

        # Use cache interpolation along the strike axis at the chosen expiry.
        entry = cache.interpolate(pm_tick.strike, best_expiry, interp_expiries=False)
        if entry is None:
            return None

        is_interpolated = (
            nearest_strike != pm_tick.strike or best_expiry != pm_tick.expiry
        )
        quality = self._quality_score(strike_gap_pct, expiry_gap_h)

        logger.debug(
            "matcher.match_found",
            contract=pm_tick.contract_id,
            strike_gap_pct=round(strike_gap_pct * 100, 3),
            expiry_gap_h=round(expiry_gap_h, 2),
            quality=round(quality, 3),
            interpolated=is_interpolated,
        )

        return MatchResult(
            pm_tick=pm_tick,
            pm_quote=pm_quote,
            options_entry=entry,
            matched_strike=nearest_strike,
            matched_expiry=best_expiry,
            strike_gap_pct=strike_gap_pct,
            expiry_gap_hours=expiry_gap_h,
            match_quality=quality,
            is_interpolated=is_interpolated,
        )

    def batch_match(
        self,
        pm_ticks: list[PredictionMarketTick],
        cache: ProbabilityCache,
    ) -> list[MatchResult]:
        """Match a batch of PM ticks; drops unmatched contracts silently."""
        results = []
        for tick in pm_ticks:
            result = self.match(tick, cache)
            if result is not None:
                results.append(result)
        return results

    # ── private ───────────────────────────────────────────────────────────

    def _quality_score(self, strike_gap_pct: float, expiry_gap_h: float) -> float:
        """Linear quality score in [0, 1].

        Each gap dimension contributes equally; quality degrades to 0 when
        either gap reaches its configured maximum.
        """
        s_pen = min(strike_gap_pct / max(self.max_strike_gap_pct, 1e-9), 1.0)
        e_pen = min(expiry_gap_h / max(self.max_expiry_gap_hours, 1e-9), 1.0)
        return max(0.0, 1.0 - (s_pen + e_pen) / 2.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pm_tick_to_quote(tick: PredictionMarketTick) -> ProbabilityQuote | None:
    """Convert a PredictionMarketTick to a ProbabilityQuote.  Returns None on missing data."""
    if (
        tick.yes_bid is None
        or tick.yes_ask is None
        or tick.strike is None
        or tick.expiry is None
    ):
        return None

    mid = tick.yes_mid
    if mid is None:
        return None

    if tick.source == DataSource.KALSHI:
        settlement_type = "kalshi_rti"
    elif tick.source == DataSource.POLYMARKET:
        settlement_type = "polymarket_spot"
    else:
        settlement_type = "unknown"

    return ProbabilityQuote(
        source=tick.source,
        contract_id=tick.contract_id,
        strike=tick.strike,
        expiry=tick.expiry,
        bid_prob=tick.yes_bid,
        ask_prob=tick.yes_ask,
        mid_prob=mid,
        direction="above",
        settlement_type=settlement_type,  # type: ignore[arg-type]
        timestamp=tick.timestamp,
    )


def cache_entry_to_options_quote(
    entry: CacheEntry,
    contract_id: str = "deribit-options",
) -> ProbabilityQuote:
    """Wrap a CacheEntry as a ProbabilityQuote for use in ArbitrageSignal."""
    return ProbabilityQuote(
        source=entry.source,
        contract_id=contract_id,
        strike=entry.strike,
        expiry=entry.expiry,
        bid_prob=entry.bid_prob,
        ask_prob=entry.ask_prob,
        mid_prob=entry.mid_prob,
        direction="above",
        settlement_type="deribit_twap",
        timestamp=entry.timestamp,
    )
