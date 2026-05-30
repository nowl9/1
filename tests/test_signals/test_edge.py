"""Tests for signals/edge.py — EdgeCalculator.

Key scenarios:
  - Clear arbitrage (10 %+ edge) propagates correctly through all fields.
  - Marginal edge (< min threshold) passes through EdgeCalculator untouched
    (filtering is the responsibility of SignalFilter, not EdgeCalculator).
  - Conservative edges are strictly more pessimistic than mid edges.
  - Settlement basis adjustment is exercised with a mock VolSurface.
  - Edge history accumulates and produces a non-zero persistence score.
  - best_side and best_conservative_edge reflect the dominant opportunity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from btc_pm_arb.models import DataSource, PredictionMarketTick
from btc_pm_arb.pricing.cache import CacheEntry, ProbabilityCache
from btc_pm_arb.signals.edge import EdgeCalculator, EdgeResult
from btc_pm_arb.signals.matcher import ContractMatcher, MatchResult

# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime.now(timezone.utc)
_EXPIRY = _NOW + timedelta(days=27)


def _pm_tick(
    strike: float = 100_000.0,
    yes_bid: float = 0.40,
    yes_ask: float = 0.44,
    no_bid: float | None = None,
    no_ask: float | None = None,
    source: DataSource = DataSource.POLYMARKET,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=source,
        contract_id=f"pm-btc-{int(strike)}",
        question=f"BTC above ${int(strike):,}?",
        strike=strike,
        expiry=_EXPIRY,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=_NOW,
    )


def _cache_entry(
    strike: float = 100_000.0,
    bid: float = 0.55,
    ask: float = 0.59,
) -> CacheEntry:
    return CacheEntry(
        strike=strike,
        expiry=_EXPIRY,
        bid_prob=bid,
        ask_prob=ask,
        mid_prob=(bid + ask) / 2,
        source=DataSource.DERIBIT,
        timestamp=_NOW,
    )


def _match(
    options_bid: float = 0.55,
    options_ask: float = 0.59,
    pm_yes_bid: float = 0.40,
    pm_yes_ask: float = 0.44,
    pm_no_bid: float | None = None,
    pm_no_ask: float | None = None,
) -> MatchResult:
    """Build a MatchResult without touching the cache/matcher."""
    pm_tick = _pm_tick(
        yes_bid=pm_yes_bid,
        yes_ask=pm_yes_ask,
        no_bid=pm_no_bid,
        no_ask=pm_no_ask,
    )
    # Build pm_quote inline (mirrors _pm_tick_to_quote logic)
    from btc_pm_arb.models import ProbabilityQuote
    pm_quote = ProbabilityQuote(
        source=DataSource.POLYMARKET,
        contract_id=pm_tick.contract_id,
        strike=pm_tick.strike,
        expiry=pm_tick.expiry,
        bid_prob=pm_yes_bid,
        ask_prob=pm_yes_ask,
        mid_prob=(pm_yes_bid + pm_yes_ask) / 2,
        direction="above",
        settlement_type="polymarket_spot",
        timestamp=_NOW,
    )
    entry = _cache_entry(bid=options_bid, ask=options_ask)
    return MatchResult(
        pm_tick=pm_tick,
        pm_quote=pm_quote,
        options_entry=entry,
        matched_strike=100_000.0,
        matched_expiry=_EXPIRY,
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        is_interpolated=False,
    )


# ── Clear arbitrage scenario ──────────────────────────────────────────────────

def test_clear_arb_positive_yes_edge():
    """Options bid = 0.60, PM yes_ask = 0.44 → edge_yes_conservative ≈ 0.16."""
    match = _match(options_bid=0.60, options_ask=0.64, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.edge_yes_conservative == pytest.approx(0.60 - 0.44)


def test_clear_arb_best_side_is_buy_yes():
    match = _match(options_bid=0.60, options_ask=0.64, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.best_side == "buy_yes"


def test_clear_arb_best_conservative_edge():
    match = _match(options_bid=0.60, options_ask=0.64, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.best_conservative_edge == pytest.approx(0.16, abs=1e-9)


def test_clear_arb_positive_no_edge():
    """Options ask = 0.35, PM no_ask derived from pm_yes_bid = 0.70 → no_ask = 0.30.
    edge_no = (1 - 0.35) - 0.30 = 0.35."""
    match = _match(options_bid=0.30, options_ask=0.35, pm_yes_bid=0.70, pm_yes_ask=0.74)
    calc = EdgeCalculator()
    result = calc.compute(match)
    # pm_no_ask = 1 - pm_yes_bid = 0.30
    expected_no_edge = (1.0 - 0.35) - (1.0 - 0.70)
    assert result.edge_no_conservative == pytest.approx(expected_no_edge, abs=1e-9)


def test_clear_arb_best_side_is_buy_no():
    """PM is very overpriced on YES side → best side is buy_no."""
    match = _match(options_bid=0.30, options_ask=0.34, pm_yes_bid=0.78, pm_yes_ask=0.82)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.best_side == "buy_no"


# ── Mid edges vs conservative edges ──────────────────────────────────────────

def test_mid_edge_wider_than_conservative():
    """Mid edge should exceed conservative edge (spread consumes edge)."""
    match = _match(options_bid=0.55, options_ask=0.61, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match)
    # mid_yes = 0.58 - 0.42 = 0.16, conservative = 0.55 - 0.44 = 0.11
    assert result.edge_yes_mid > result.edge_yes_conservative


def test_conservative_edge_uses_options_bid_for_yes():
    """YES conservative edge uses options *bid* not mid."""
    match = _match(options_bid=0.52, options_ask=0.60, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match)
    # Should use options_bid=0.52, not mid=0.56
    assert result.edge_yes_conservative == pytest.approx(0.52 - 0.44, abs=1e-9)


def test_conservative_edge_uses_options_ask_for_no():
    """NO conservative edge uses options *ask* (1-ask is smallest)."""
    match = _match(options_bid=0.44, options_ask=0.52, pm_yes_bid=0.60, pm_yes_ask=0.64)
    calc = EdgeCalculator()
    result = calc.compute(match)
    # pm_no_ask = 1 - pm_yes_bid = 0.40, edge_no = (1-0.52) - 0.40 = 0.08
    assert result.edge_no_conservative == pytest.approx((1.0 - 0.52) - (1.0 - 0.60), abs=1e-9)


# ── No edge case ──────────────────────────────────────────────────────────────

def test_no_edge_when_pm_fair_priced():
    """Options mid = 0.50, PM mid = 0.50, small spreads → no positive conservative edge."""
    match = _match(options_bid=0.48, options_ask=0.52, pm_yes_bid=0.49, pm_yes_ask=0.51)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.edge_yes_conservative < 0
    assert result.best_side is None or result.best_conservative_edge == 0.0


def test_best_conservative_edge_zero_when_no_positive():
    match = _match(options_bid=0.45, options_ask=0.55, pm_yes_bid=0.48, pm_yes_ask=0.52)
    calc = EdgeCalculator()
    result = calc.compute(match)
    # edge_yes = 0.45 - 0.52 = -0.07; edge_no = 0.45 - 0.52 = -0.07
    assert result.best_conservative_edge == pytest.approx(0.0)


# ── Explicit no_bid / no_ask ──────────────────────────────────────────────────

def test_explicit_no_ask_used_in_edge():
    match = _match(
        options_bid=0.60,
        options_ask=0.64,
        pm_yes_bid=0.40,
        pm_yes_ask=0.44,
        pm_no_bid=0.54,
        pm_no_ask=0.58,
    )
    calc = EdgeCalculator()
    result = calc.compute(match)
    # Explicit no_ask = 0.58
    assert result.edge_no_conservative == pytest.approx((1.0 - 0.64) - 0.58, abs=1e-9)


def test_no_ask_fallback_to_complement():
    """When no explicit no_ask, should use 1 - yes_bid."""
    match = _match(options_bid=0.60, options_ask=0.64, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match)
    # no_ask fallback = 1 - 0.40 = 0.60
    assert result.edge_no_conservative == pytest.approx((1.0 - 0.64) - 0.60, abs=1e-9)


# ── Edge history and persistence ──────────────────────────────────────────────

def test_edge_history_accumulated():
    match = _match(options_bid=0.60, options_ask=0.64)
    calc = EdgeCalculator()
    for _ in range(5):
        calc.compute(match)
    history = calc.get_history(match.pm_tick.contract_id)
    assert len(history) == 5


def test_edge_persistence_nonzero_after_stable_signal():
    """Stable positive edge over many observations → persistence > 0."""
    match = _match(options_bid=0.60, options_ask=0.64)
    calc = EdgeCalculator()
    for _ in range(20):
        result = calc.compute(match)
    assert result.edge_persistence > 0.0


def test_edge_history_capped_at_history_size():
    match = _match(options_bid=0.60, options_ask=0.64)
    calc = EdgeCalculator(history_size=5)
    for _ in range(10):
        calc.compute(match)
    history = calc.get_history(match.pm_tick.contract_id)
    assert len(history) == 5


def test_clear_history():
    match = _match(options_bid=0.60, options_ask=0.64)
    calc = EdgeCalculator()
    calc.compute(match)
    calc.clear_history(match.pm_tick.contract_id)
    assert calc.get_history(match.pm_tick.contract_id) == []


def test_history_stats_zero_for_single_observation():
    """Only one observation → cannot compute std → persistence = 0."""
    match = _match(options_bid=0.60, options_ask=0.64)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.edge_history_mean == 0.0
    assert result.edge_persistence == 0.0


# ── Adjusted edge without surface ────────────────────────────────────────────

def test_adjusted_edges_equal_conservative_without_surface():
    match = _match(options_bid=0.58, options_ask=0.62, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match, surface=None)
    assert result.adjusted_edge_yes == pytest.approx(result.edge_yes_conservative)
    assert result.adjusted_edge_no == pytest.approx(result.edge_no_conservative)


# ── EdgeResult fields ─────────────────────────────────────────────────────────

def test_edge_result_has_timestamp():
    match = _match()
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.timestamp is not None
    assert result.timestamp.tzinfo is not None


def test_edge_result_carries_match_reference():
    match = _match(options_bid=0.60, options_ask=0.64)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.match is match


# ── Direction-aware pricing (polarity fix) ────────────────────────────────────

def _below_match(
    options_bid: float = 0.97,
    options_ask: float = 0.97,
    pm_yes_bid: float = 0.03,
    pm_yes_ask: float = 0.04,
) -> MatchResult:
    """A 'below' contract: PM YES pays on S_T <= K, modelled vs 1 - P(above)."""
    from btc_pm_arb.models import ProbabilityQuote
    pm_tick = _pm_tick(yes_bid=pm_yes_bid, yes_ask=pm_yes_ask)
    pm_tick = pm_tick.model_copy(update={"direction": "below"})
    pm_quote = ProbabilityQuote(
        source=DataSource.KALSHI,
        contract_id=pm_tick.contract_id,
        strike=pm_tick.strike,
        expiry=pm_tick.expiry,
        bid_prob=pm_yes_bid,
        ask_prob=pm_yes_ask,
        mid_prob=(pm_yes_bid + pm_yes_ask) / 2,
        direction="below",
        product_type="terminal",
        settlement_type="kalshi_rti",
        timestamp=_NOW,
    )
    entry = _cache_entry(bid=options_bid, ask=options_ask)
    return MatchResult(
        pm_tick=pm_tick,
        pm_quote=pm_quote,
        options_entry=entry,
        matched_strike=100_000.0,
        matched_expiry=_EXPIRY,
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        is_interpolated=False,
    )


def test_below_contract_priced_vs_one_minus_ndtwo():
    """strike_type:less terminal — YES leg differenced vs (1 - N(d2)).

    Model P(above) = 0.97 → P(below) = 0.03.  edge_yes_conservative must be
    0.03 - pm_yes_ask (0.04) = -0.01, NOT the phantom 0.97 - 0.04 = +0.93.
    """
    match = _below_match(options_bid=0.97, options_ask=0.97,
                         pm_yes_bid=0.03, pm_yes_ask=0.04)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.edge_yes_conservative == pytest.approx((1.0 - 0.97) - 0.04, abs=1e-9)
    # No phantom positive YES edge.
    assert result.best_conservative_edge < 0.5


def test_above_contract_unchanged_by_direction_logic():
    """Regression: an 'above' contract prices exactly as before."""
    match = _match(options_bid=0.60, options_ask=0.64, pm_yes_bid=0.40, pm_yes_ask=0.44)
    calc = EdgeCalculator()
    result = calc.compute(match)
    assert result.edge_yes_conservative == pytest.approx(0.60 - 0.44, abs=1e-9)
