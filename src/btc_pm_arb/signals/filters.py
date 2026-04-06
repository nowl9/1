"""Signal filter — applies configurable threshold criteria to EdgeResult objects.

Each criterion is a pure function; adding a new criterion means adding a
function and inserting it into the ``_CRITERIA`` list — no other changes needed.

Filter criteria (all configurable via FilterConfig):
  1. min_conservative_edge  — reject if best adjusted edge < threshold (default 3 %)
  2. min_days_to_expiry     — reject contracts expiring < 1 day (extreme gamma risk)
  3. max_days_to_expiry     — reject contracts expiring > 90 days (stale vol surface)
  4. max_pm_spread          — reject if PM bid-ask spread > threshold (poor liquidity)
  5. min_match_quality      — reject low-quality option-to-PM matches
  6. max_vol_fit_rmse       — reject when SVI fit quality is poor (unreliable probs)
  7. stale_data             — reject when cache entry is older than max_data_age_seconds

Passing signals are converted to ``ArbitrageSignal`` objects (from models.py) and
ranked by adjusted edge descending.  The confidence field is left at its default
0.5 to be filled in by ConfidenceScorer downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import structlog

from btc_pm_arb.models import ArbitrageSignal, DataSource, ProbabilityQuote
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.signals.edge import EdgeResult
from btc_pm_arb.signals.matcher import cache_entry_to_options_quote

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class FilterConfig:
    """Configurable thresholds for the signal filter chain."""

    # Edge thresholds
    min_conservative_edge: float = 0.03    # 3 % — minimum post-spread edge
    min_mid_edge: float = 0.01             # 1 % — also require a mid edge

    # Time-to-expiry bounds (in days)
    min_days_to_expiry: float = 1.0        # avoid expiry-day gamma spikes
    max_days_to_expiry: float = 90.0       # avoid stale vol surface coverage

    # Prediction market liquidity
    max_pm_spread: float = 0.12            # max YES bid-ask spread (12 %)
    min_pm_liquidity_usd: float = 50.0     # min estimated notional at quoted price

    # Data quality
    min_match_quality: float = 0.40        # minimum match score from ContractMatcher
    max_vol_fit_rmse: float = 0.05         # max SVI RMSE (5 vol-pts as a fraction)
    max_data_age_seconds: float = 300.0    # 5-minute staleness cutoff

    # Position concentration (used for correlated-exposure filter)
    max_position_usd: float = 1_000.0
    max_correlated_exposure_usd: float = 5_000.0
    correlated_strike_band_pct: float = 0.10   # strikes within 10 % are "correlated"


# ── Criterion functions ───────────────────────────────────────────────────────

# Each criterion takes (EdgeResult, FilterConfig, context dict) → rejection reason str or None
_Criterion = Callable[["EdgeResult", FilterConfig, dict], str | None]


def _reject_no_edge(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    if e.best_side is None or e.best_conservative_edge <= 0:
        return "no_positive_edge"
    return None


def _reject_min_conservative_edge(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    if e.best_conservative_edge < cfg.min_conservative_edge:
        return f"conservative_edge {e.best_conservative_edge:.4f} < min {cfg.min_conservative_edge}"
    return None


def _reject_min_mid_edge(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    mid = e.edge_yes_mid if e.best_side == "buy_yes" else e.edge_no_mid
    if mid < cfg.min_mid_edge:
        return f"mid_edge {mid:.4f} < min {cfg.min_mid_edge}"
    return None


def _reject_expiry_bounds(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    expiry = e.match.pm_tick.expiry
    if expiry is None:
        return "no_expiry"
    now = datetime.now(timezone.utc)
    days = (expiry - now).total_seconds() / 86400.0
    if days < cfg.min_days_to_expiry:
        return f"days_to_expiry {days:.2f} < min {cfg.min_days_to_expiry}"
    if days > cfg.max_days_to_expiry:
        return f"days_to_expiry {days:.1f} > max {cfg.max_days_to_expiry}"
    return None


def _reject_pm_spread(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    spread = e.match.pm_quote.ask_prob - e.match.pm_quote.bid_prob
    if spread > cfg.max_pm_spread:
        return f"pm_spread {spread:.4f} > max {cfg.max_pm_spread}"
    return None


def _reject_match_quality(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    if e.match.match_quality < cfg.min_match_quality:
        return f"match_quality {e.match.match_quality:.3f} < min {cfg.min_match_quality}"
    return None


def _reject_vol_fit(e: EdgeResult, cfg: FilterConfig, ctx: dict) -> str | None:
    surface: VolSurface | None = ctx.get("surface")
    if surface is None:
        return None   # can't check without surface — pass
    smile = surface.get_smile(e.match.matched_expiry)
    if smile is None:
        return None   # no smile data — pass (surface may not cover this expiry)
    if smile.fit_rmse > cfg.max_vol_fit_rmse:
        return f"vol_rmse {smile.fit_rmse:.4f} > max {cfg.max_vol_fit_rmse}"
    return None


def _reject_stale_data(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    now = datetime.now(timezone.utc)
    age = (now - e.match.options_entry.timestamp).total_seconds()
    if age > cfg.max_data_age_seconds:
        return f"options_data_age {age:.0f}s > max {cfg.max_data_age_seconds}s"
    # Also check PM data age
    pm_age = (now - e.match.pm_tick.timestamp).total_seconds()
    if pm_age > cfg.max_data_age_seconds:
        return f"pm_data_age {pm_age:.0f}s > max {cfg.max_data_age_seconds}s"
    return None


def _reject_correlated_exposure(
    e: EdgeResult, cfg: FilterConfig, ctx: dict
) -> str | None:
    """Reject if adding this position would exceed the correlated exposure cap.

    Positions within ``correlated_strike_band_pct`` of each other count toward
    the same exposure bucket.
    """
    positions: dict[str, float] = ctx.get("positions", {})
    if not positions:
        return None

    K = e.match.pm_tick.strike
    if K is None:
        return None

    band = cfg.correlated_strike_band_pct
    corr_exposure = sum(
        notional
        for contract_id, notional in positions.items()
        if _strike_from_id(contract_id, K) is not None
        and abs(_strike_from_id(contract_id, K) - K) / K <= band  # type: ignore[operator]
    )
    if corr_exposure + cfg.max_position_usd > cfg.max_correlated_exposure_usd:
        return (
            f"correlated_exposure {corr_exposure:.0f} + {cfg.max_position_usd:.0f} "
            f"> cap {cfg.max_correlated_exposure_usd:.0f}"
        )
    return None


_CRITERIA: list[_Criterion] = [
    _reject_no_edge,
    _reject_min_conservative_edge,
    _reject_min_mid_edge,
    _reject_expiry_bounds,
    _reject_pm_spread,
    _reject_match_quality,
    _reject_vol_fit,
    _reject_stale_data,
    _reject_correlated_exposure,
]


# ── Filter class ──────────────────────────────────────────────────────────────

class SignalFilter:
    """Apply the criterion chain and convert surviving EdgeResults to ArbitrageSignals.

    Usage::

        filt = SignalFilter(FilterConfig(min_conservative_edge=0.04))
        signals = filt.filter(edge_results, surface=surface)
    """

    def __init__(self, config: FilterConfig | None = None) -> None:
        self.config = config or FilterConfig()

    def filter(
        self,
        edge_results: list[EdgeResult],
        surface: VolSurface | None = None,
        positions: dict[str, float] | None = None,
    ) -> list[ArbitrageSignal]:
        """Filter EdgeResults and return ranked ArbitrageSignal list.

        Args:
            edge_results: Output of EdgeCalculator.compute() calls.
            surface:      VolSurface for vol-quality criterion.
            positions:    Existing positions {contract_id: notional_usd} for
                          correlated-exposure filtering.

        Returns:
            Signals ranked by adjusted_edge descending (best opportunity first).
        """
        ctx: dict = {"surface": surface, "positions": positions or {}}

        signals: list[ArbitrageSignal] = []
        for edge in edge_results:
            rejection = self._first_rejection(edge, ctx)
            if rejection is not None:
                logger.debug(
                    "signal.filtered",
                    contract=edge.match.pm_tick.contract_id,
                    reason=rejection,
                )
                continue
            signals.append(_to_arbitrage_signal(edge))

        signals.sort(key=lambda s: s.adjusted_edge, reverse=True)
        return signals

    def explains(
        self,
        edge: EdgeResult,
        surface: VolSurface | None = None,
        positions: dict[str, float] | None = None,
    ) -> str | None:
        """Return the first rejection reason for a single EdgeResult, or None if it passes."""
        ctx: dict = {"surface": surface, "positions": positions or {}}
        return self._first_rejection(edge, ctx)

    # ── private ───────────────────────────────────────────────────────────

    def _first_rejection(self, edge: EdgeResult, ctx: dict) -> str | None:
        for criterion in _CRITERIA:
            reason = criterion(edge, self.config, ctx)
            if reason is not None:
                return reason
        return None


# ── Conversion helper ─────────────────────────────────────────────────────────

def _to_arbitrage_signal(edge: EdgeResult) -> ArbitrageSignal:
    """Convert a passing EdgeResult to an ArbitrageSignal (confidence = 0.5 placeholder)."""
    options_quote = cache_entry_to_options_quote(
        edge.match.options_entry,
        contract_id=edge.match.pm_tick.contract_id,
    )
    raw = edge.edge_yes_mid if edge.best_side == "buy_yes" else edge.edge_no_mid
    return ArbitrageSignal(
        options_quote=options_quote,
        pm_quote=edge.match.pm_quote,
        raw_edge=raw,
        adjusted_edge=edge.best_conservative_edge,
        trade_side=edge.best_side,  # type: ignore[arg-type]
        confidence=0.5,
        timestamp=edge.timestamp,
    )


def _strike_from_id(contract_id: str, fallback: float | None) -> float | None:
    """Best-effort strike extraction from a contract_id string for exposure check."""
    # Contract IDs in this system embed the strike when possible; fallback gracefully
    return fallback   # For MVP: treat all positions as at the same strike bucket
