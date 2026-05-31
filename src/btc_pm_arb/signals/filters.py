"""Signal filter — applies configurable threshold criteria to EdgeResult objects.

Each criterion is a pure function; adding a new criterion means adding a
function and inserting it into the ``_CRITERIA`` list — no other changes needed.

Filter criteria (all configurable via FilterConfig):
  1.  no_edge               — reject if no positive edge exists
  2.  min_conservative_edge — reject if best adjusted edge < threshold (default 3 %)
  3.  min_mid_edge          — reject if mid edge < threshold (1 %)
  4.  expiry_bounds         — reject contracts too close or too far from expiry
  5.  pm_spread             — reject if PM bid-ask spread > threshold (12 %)
  6.  match_quality         — reject low-quality option-to-PM matches
  7.  vol_fit               — reject when SVI fit quality is poor (unreliable probs)
  8.  stale_data            — reject when cache entry is older than max_data_age_seconds
  9.  correlated_exposure   — reject if adding would exceed correlated exposure cap
  10. feed_freshness        — reject if ANY upstream feed is staler than threshold
  11. odds_velocity         — reject if PM price is converging toward implied prob at speed
  12. vol_regime_edge       — adjust minimum edge threshold by volatility regime

Passing signals are converted to ``ArbitrageSignal`` objects (from models.py) and
ranked by adjusted edge descending.  The confidence field is left at its default
0.5 to be filled in by ConfidenceScorer downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

import structlog

from btc_pm_arb.models import ArbitrageSignal, DataSource, ProbabilityQuote
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.signals.edge import EdgeResult
from btc_pm_arb.signals.matcher import cache_entry_to_options_quote

if TYPE_CHECKING:
    from btc_pm_arb.feeds.health import FeedHealthTracker
    from btc_pm_arb.pricing.realized_vol import RealizedVolTracker, VolRegime
    from btc_pm_arb.signals.velocity import OddsVelocityTracker

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class FilterConfig:
    """Configurable thresholds for the signal filter chain."""

    # Edge thresholds
    # Round 9 Commit 9a: lowered from 3% / 1% to unblock paper-trade data
    # flow for Round 9c calibration.  At the old thresholds the ledger
    # stayed empty after a 5-min smoke test.  1% is a "pipeline noise
    # floor" — below it the signal is plausibly artifact of basis-adjuster
    # approximation error, BS-to-probability rounding, or IV smile
    # interpolation.  0.5% mid is the proportional companion (mid is
    # naturally laxer than conservative; halving keeps it as a sanity
    # check, not the binding constraint).  These are data-collection
    # floors, NOT calibrated thresholds; Round 9c replaces them with
    # values backed by realized P&L analysis on the accumulated dataset.
    min_conservative_edge: float = 0.01    # 1 % — minimum post-spread edge
    min_mid_edge: float = 0.005            # 0.5 % — also require a mid edge

    # Time-to-expiry bounds (in days)
    min_days_to_expiry: float = 1.0        # avoid expiry-day gamma spikes
    max_days_to_expiry: float = 90.0       # avoid stale vol surface coverage

    # Prediction market liquidity
    max_pm_spread: float = 0.12            # max YES bid-ask spread (12 %)
    min_pm_liquidity_usd: float = 50.0     # min estimated notional at quoted price

    # Depth gate: require the crossed book side to carry at least one level.
    # A quote with order_book == None means depth is unknown (gate skipped);
    # an explicit empty list means known-empty and is rejected.  The diag
    # (outputs/diag_0p93_signal.md) showed an empty-book signal passing all
    # gates and placing a dry-run order.
    require_nonempty_book: bool = True

    # Data quality
    min_match_quality: float = 0.40        # minimum match score from ContractMatcher
    max_vol_fit_rmse: float = 0.05         # max SVI RMSE (5 vol-pts as a fraction)
    max_data_age_seconds: float = 300.0    # 5-minute staleness cutoff

    # Position concentration (used for correlated-exposure filter)
    max_position_usd: float = 1_000.0
    max_correlated_exposure_usd: float = 5_000.0
    correlated_strike_band_pct: float = 0.10   # strikes within 10 % are "correlated"

    # ── Enhancement 1a: Data Freshness Gate ───────────────────────────────────
    max_deribit_staleness_s: float = 5.0       # Deribit WS tick recency
    max_pm_staleness_s: float = 15.0           # Polymarket / Kalshi tick recency

    # ── Enhancement 1b: Odds Velocity Gate ───────────────────────────────────
    # Reject if the PM price is converging AND |velocity| > this threshold
    odds_velocity_threshold: float = 1e-3      # prob/second (0.1 %/s)

    # ── Enhancement 1c: Volatility Regime Filter ──────────────────────────────
    # When a RealizedVolTracker is provided, the base min_conservative_edge is
    # multiplied by the regime factor (0.8 / 1.0 / 1.5).  This gate uses the
    # regime-adjusted threshold rather than a hard rejection.
    use_vol_regime_adjustment: bool = True


# ── Criterion functions ───────────────────────────────────────────────────────

# Each criterion takes (EdgeResult, FilterConfig, context dict) → rejection reason str or None
_Criterion = Callable[["EdgeResult", FilterConfig, dict], str | None]


def _ctx_now(ctx: dict) -> datetime:
    """Return "now" via the injected sim-clock seam, else wall-clock.

    Build step 1 (Fork 3): the freshness gates read ``ctx["clock"]`` (a
    ``Callable[[], datetime]``, typically a
    :class:`btc_pm_arb.clock.SimulatedClock`) instead of calling
    ``datetime.now(timezone.utc)`` inline.  Absent a clock — the default for
    every existing caller — this falls back to wall-clock, so default-live
    behaviour is unchanged.
    """
    clock = ctx.get("clock")
    if clock is not None:
        return clock()
    return datetime.now(timezone.utc)


def _reject_one_touch(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    """Reject path-dependent one-touch barriers (track but never signal).

    The pricer computes a terminal European digital P(S_T > K); a one-touch
    barrier settles if the level is EVER breached, a different product whose
    terminal-vs-barrier mismatch produces phantom edges (see
    outputs/diag_0p93_signal.md).  Skip until a barrier pricer exists.
    """
    if e.match.pm_quote.product_type == "one_touch":
        return "one_touch_barrier"
    return None


def _reject_range(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    """Reject band / range products (track but never signal).

    A "between $X and $Y" market pays on the terminal price landing inside a
    band -- a different payoff from the single-threshold digital the pricer
    computes (P(S_T > K)).  Pricing it against one strike produces a phantom
    edge, so it is excluded until a range pricer exists (mirrors
    _reject_one_touch; see outputs/fix_pm_classifier_report.md).
    """
    if e.match.pm_quote.product_type == "range":
        return "range_product"
    return None


def _reject_no_edge(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    if e.best_side is None or e.best_conservative_edge <= 0:
        return "no_positive_edge"
    return None


def _reject_empty_book(e: EdgeResult, cfg: FilterConfig, _: dict) -> str | None:
    """Reject signals whose crossed book side is explicitly empty.

    ``best_side == "buy_yes"`` executes against the YES book; ``"buy_no"``
    against the NO book.  ``order_book_* is None`` means the depth was never
    captured (skip — don't penalise missing data); an empty list means the
    book was captured and is empty (no liquidity to trade into → reject).
    """
    if not cfg.require_nonempty_book or e.best_side is None:
        return None
    q = e.match.pm_quote
    book = q.order_book_yes if e.best_side == "buy_yes" else q.order_book_no
    if book is None:
        return None
    if len(book) == 0:
        return "empty_book"
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


def _reject_expiry_bounds(e: EdgeResult, cfg: FilterConfig, ctx: dict) -> str | None:
    expiry = e.match.pm_tick.expiry
    if expiry is None:
        return "no_expiry"
    now = _ctx_now(ctx)
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


def _reject_stale_data(e: EdgeResult, cfg: FilterConfig, ctx: dict) -> str | None:
    now = _ctx_now(ctx)
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


def _reject_feed_freshness(e: EdgeResult, cfg: FilterConfig, ctx: dict) -> str | None:
    """Reject if any upstream feed has gone stale beyond its configured threshold.

    Requires ``ctx["feed_health"]`` → ``FeedHealthTracker``.
    Skipped (pass) when no health tracker is present in context.
    """
    health: FeedHealthTracker | None = ctx.get("feed_health")
    if health is None:
        return None
    deribit_s = health.staleness_s(DataSource.DERIBIT)
    if deribit_s > cfg.max_deribit_staleness_s:
        return (
            f"deribit_feed_stale {deribit_s:.1f}s > max {cfg.max_deribit_staleness_s}s"
        )
    pm_source = e.match.pm_tick.source
    pm_s = health.staleness_s(pm_source)
    if pm_s > cfg.max_pm_staleness_s:
        return f"{pm_source.value}_feed_stale {pm_s:.1f}s > max {cfg.max_pm_staleness_s}s"
    return None


def _reject_odds_velocity(e: EdgeResult, cfg: FilterConfig, ctx: dict) -> str | None:
    """Reject if PM price is converging toward implied probability at speed.

    Requires ``ctx["odds_tracker"]`` → ``OddsVelocityTracker``.
    Skipped when no tracker is present in context.

    Logic:
    * converging + |v| > threshold → reject (edge is closing, don't chase)
    * diverging + |v| > threshold → pass (edge is widening; confidence is set
      in ConfidenceScorer, not here)
    """
    tracker: OddsVelocityTracker | None = ctx.get("odds_tracker")
    if tracker is None:
        return None
    implied = e.match.options_entry.mid_prob
    result = tracker.velocity_at(e.match.pm_tick.contract_id, implied_prob=implied)
    if result is None:
        return None
    if result.direction == "converging" and abs(result.velocity) > cfg.odds_velocity_threshold:
        return (
            f"odds_converging velocity={result.velocity:.5f} > "
            f"threshold {cfg.odds_velocity_threshold}"
        )
    return None


def _reject_vol_regime_edge(e: EdgeResult, cfg: FilterConfig, ctx: dict) -> str | None:
    """Apply regime-adjusted minimum edge threshold.

    Requires ``ctx["rv_tracker"]`` → ``RealizedVolTracker``.
    Skipped (uses base min_edge) when no tracker provided.

    Regime multipliers (from realized_vol.py):
      LOW:    0.8  (tighter threshold acceptable in calm markets)
      NORMAL: 1.0  (base threshold)
      HIGH:   1.5  (wider edge required; elevated basis risk)
    """
    if not cfg.use_vol_regime_adjustment:
        return None
    rv_tracker: RealizedVolTracker | None = ctx.get("rv_tracker")
    if rv_tracker is None:
        return None
    adj_min = rv_tracker.effective_min_edge(cfg.min_conservative_edge)
    if e.best_conservative_edge < adj_min:
        regime = rv_tracker.current_regime()
        return (
            f"regime_adjusted_edge {e.best_conservative_edge:.4f} < "
            f"regime_min {adj_min:.4f} (regime={regime})"
        )
    return None


_CRITERIA: list[_Criterion] = [
    _reject_one_touch,
    _reject_range,
    _reject_no_edge,
    _reject_empty_book,
    _reject_min_conservative_edge,
    _reject_min_mid_edge,
    _reject_expiry_bounds,
    _reject_pm_spread,
    _reject_match_quality,
    _reject_vol_fit,
    _reject_stale_data,
    _reject_correlated_exposure,
    # Enhancement gates (skipped if their context keys are absent)
    _reject_feed_freshness,
    _reject_odds_velocity,
    _reject_vol_regime_edge,
]


# ── Filter class ──────────────────────────────────────────────────────────────

# Reasons returned by criterion functions are formatted as
# ``"<key> <details>"`` (e.g., ``"conservative_edge 0.0050 < min 0.01"``)
# or as bare keys (e.g., ``"no_positive_edge"``).  ``_extract_reason_key``
# returns the first whitespace-delimited token — the stable bucket name
# used in :attr:`SignalFilter.rejection_counts`.  Keeping the full message
# in DEBUG logs for forensics; using the bucket key for telemetry.
def _extract_reason_key(reason: str) -> str:
    """Return the stable bucket key for a rejection reason string."""
    return reason.split(maxsplit=1)[0] if reason else "unknown"


class SignalFilter:
    """Apply the criterion chain and convert surviving EdgeResults to ArbitrageSignals.

    Usage::

        filt = SignalFilter(FilterConfig(min_conservative_edge=0.04))
        signals = filt.filter(edge_results, surface=surface)

    Rejection telemetry
    -------------------
    :attr:`rejection_counts` is a cumulative counter of rejection reasons
    over the lifetime of the instance, keyed by the stable bucket name
    extracted from each reason string (see :func:`_extract_reason_key`).
    Counters are incremented inside :meth:`filter` only — :meth:`explains`
    is diagnostic and does NOT increment, so the same edge being inspected
    via ``explains()`` after being rejected by ``filter()`` won't double-
    count.  Surfaced on the dashboard via ``main.Agent`` so operators can
    see at a glance which gate is the dominant blocker.  Cumulative across
    the agent's process lifetime; restart resets.
    """

    def __init__(self, config: FilterConfig | None = None) -> None:
        self.config = config or FilterConfig()
        # Cumulative counter keyed by reason bucket (e.g., "conservative_edge",
        # "pm_spread", "deribit_feed_stale").  See class docstring.
        self.rejection_counts: dict[str, int] = {}

    def filter(
        self,
        edge_results: list[EdgeResult],
        surface: VolSurface | None = None,
        positions: dict[str, float] | None = None,
        feed_health: "FeedHealthTracker | None" = None,
        odds_tracker: "OddsVelocityTracker | None" = None,
        rv_tracker: "RealizedVolTracker | None" = None,
        clock: "Callable[[], datetime] | None" = None,
    ) -> list[ArbitrageSignal]:
        """Filter EdgeResults and return ranked ArbitrageSignal list.

        Args:
            edge_results:  Output of EdgeCalculator.compute() calls.
            surface:       VolSurface for vol-quality criterion.
            positions:     Existing positions {contract_id: notional_usd} for
                           correlated-exposure filtering.
            feed_health:   FeedHealthTracker for the Data Freshness Gate.
            odds_tracker:  OddsVelocityTracker for the Odds Velocity Gate.
            rv_tracker:    RealizedVolTracker for the Vol Regime Filter.
            clock:         Injectable "now" source for the freshness/expiry
                           gates (build step 1, Fork 3).  Defaults to
                           wall-clock when None — default-live unchanged.

        Returns:
            Signals ranked by adjusted_edge descending (best opportunity first).
        """
        ctx: dict = {
            "surface": surface,
            "positions": positions or {},
            "feed_health": feed_health,
            "odds_tracker": odds_tracker,
            "rv_tracker": rv_tracker,
            "clock": clock,
        }

        signals: list[ArbitrageSignal] = []
        for edge in edge_results:
            rejection = self._first_rejection(edge, ctx)
            if rejection is not None:
                # Increment cumulative telemetry counter (filter-pass only —
                # explains() does NOT increment; see class docstring).
                key = _extract_reason_key(rejection)
                self.rejection_counts[key] = self.rejection_counts.get(key, 0) + 1
                logger.debug(
                    "signal.filtered",
                    contract=edge.match.pm_tick.contract_id,
                    reason=rejection,
                )
                continue
            signals.append(_to_arbitrage_signal(edge, feed_health, rv_tracker))
            # Round 9d2: emit an INFO event for every signal that survives
            # the full criterion chain.  This is the passing-side counterpart
            # to the DEBUG "signal.filtered" event above and the funnel's
            # observable proof that discovery → matching → filtering reaches
            # a tradable contract.  Field names are read straight off the
            # source dataclasses (do not rename):
            #   venue         ← PredictionMarketTick.source (DataSource enum)
            #   contract      ← PredictionMarketTick.contract_id (the ticker)
            #   adjusted_edge ← EdgeResult.best_conservative_edge — the exact
            #                   value _to_arbitrage_signal copies into
            #                   ArbitrageSignal.adjusted_edge
            # dte_days is derived from the PM expiry the same way
            # _reject_expiry_bounds computes it; None when expiry is missing
            # (the expiry_bounds criterion would have rejected a None expiry,
            # so survivors normally carry one — the guard is defensive).
            _expiry = edge.match.pm_tick.expiry
            _dte_days = (
                (_expiry - _ctx_now(ctx)).total_seconds() / 86400.0
                if _expiry is not None
                else None
            )
            logger.info(
                "signal.passed",
                venue=edge.match.pm_tick.source.value,
                contract=edge.match.pm_tick.contract_id,
                dte_days=round(_dte_days, 3) if _dte_days is not None else None,
                adjusted_edge=round(edge.best_conservative_edge, 6),
            )

        signals.sort(key=lambda s: s.adjusted_edge, reverse=True)
        return signals

    def explains(
        self,
        edge: EdgeResult,
        surface: VolSurface | None = None,
        positions: dict[str, float] | None = None,
        feed_health: "FeedHealthTracker | None" = None,
        odds_tracker: "OddsVelocityTracker | None" = None,
        rv_tracker: "RealizedVolTracker | None" = None,
        clock: "Callable[[], datetime] | None" = None,
    ) -> str | None:
        """Return the first rejection reason for a single EdgeResult, or None if it passes."""
        ctx: dict = {
            "surface": surface,
            "positions": positions or {},
            "feed_health": feed_health,
            "odds_tracker": odds_tracker,
            "rv_tracker": rv_tracker,
            "clock": clock,
        }
        return self._first_rejection(edge, ctx)

    # ── private ───────────────────────────────────────────────────────────

    def _first_rejection(self, edge: EdgeResult, ctx: dict) -> str | None:
        for criterion in _CRITERIA:
            reason = criterion(edge, self.config, ctx)
            if reason is not None:
                return reason
        return None


# ── Conversion helper ─────────────────────────────────────────────────────────

def _to_arbitrage_signal(
    edge: EdgeResult,
    feed_health: "FeedHealthTracker | None" = None,
    rv_tracker: "RealizedVolTracker | None" = None,
) -> ArbitrageSignal:
    """Convert a passing EdgeResult to an ArbitrageSignal (confidence = 0.5 placeholder)."""
    options_quote = cache_entry_to_options_quote(
        edge.match.options_entry,
        contract_id=edge.match.pm_tick.contract_id,
    )
    raw = edge.edge_yes_mid if edge.best_side == "buy_yes" else edge.edge_no_mid
    staleness = feed_health.all_staleness_ms() if feed_health is not None else {}
    regime = rv_tracker.current_regime().value if rv_tracker is not None else "normal"
    return ArbitrageSignal(
        options_quote=options_quote,
        pm_quote=edge.match.pm_quote,
        raw_edge=raw,
        adjusted_edge=edge.best_conservative_edge,
        fill_adjusted_edge=edge.fill_adjusted_edge,
        trade_side=edge.best_side,  # type: ignore[arg-type]
        confidence=0.5,
        feed_staleness_ms=staleness,
        vol_regime=regime,
        timestamp=edge.timestamp,
    )


def _strike_from_id(contract_id: str, fallback: float | None) -> float | None:
    """Best-effort strike extraction from a contract_id string for exposure check."""
    # Contract IDs in this system embed the strike when possible; fallback gracefully
    return fallback   # For MVP: treat all positions as at the same strike bucket
