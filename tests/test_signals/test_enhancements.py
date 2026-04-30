"""Tests for signal layer enhancements:
  - signals/velocity.py  — OddsVelocityTracker
  - feeds/health.py      — FeedHealthTracker
  - signals/filters.py   — 3 new gates (feed freshness, odds velocity, vol regime)
  - signals/edge.py      — fill-adjusted edge
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from btc_pm_arb.feeds.health import FeedHealthTracker
from btc_pm_arb.models import DataSource, PredictionMarketTick, ProbabilityQuote
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.pricing.realized_vol import RealizedVolTracker, VolRegime
from btc_pm_arb.signals.edge import EdgeCalculator, EdgeResult, fill_adjusted_price
from btc_pm_arb.signals.filters import FilterConfig, SignalFilter
from btc_pm_arb.signals.matcher import MatchResult
from btc_pm_arb.signals.velocity import OddsVelocityTracker, VelocityResult

_NOW = datetime.now(timezone.utc)
_EXPIRY = _NOW + timedelta(days=14)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pm_tick(yes_bid: float = 0.40, yes_ask: float = 0.44) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=DataSource.KALSHI,
        contract_id="pm-btc-100000",
        question="BTC above $100k?",
        strike=100_000.0,
        expiry=_EXPIRY,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        timestamp=_NOW,
    )


def _pm_quote(yes_bid: float = 0.40, yes_ask: float = 0.44) -> ProbabilityQuote:
    return ProbabilityQuote(
        source=DataSource.KALSHI,
        contract_id="pm-btc-100000",
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=yes_bid,
        ask_prob=yes_ask,
        mid_prob=(yes_bid + yes_ask) / 2,
        settlement_type="kalshi_rti",
        timestamp=_NOW,
    )


def _cache_entry(bid: float = 0.56, ask: float = 0.60, ts: datetime | None = None) -> CacheEntry:
    return CacheEntry(
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=bid,
        ask_prob=ask,
        mid_prob=(bid + ask) / 2,
        source=DataSource.DERIBIT,
        timestamp=ts or _NOW,
    )


def _match(
    options_bid: float = 0.56,
    options_ask: float = 0.60,
    pm_yes_bid: float = 0.40,
    pm_yes_ask: float = 0.44,
    options_ts: datetime | None = None,
    pm_ts: datetime | None = None,
    ob_yes: list | None = None,
    ob_no: list | None = None,
) -> MatchResult:
    tick = _pm_tick(yes_bid=pm_yes_bid, yes_ask=pm_yes_ask)
    if ob_yes is not None:
        tick = tick.model_copy(update={"order_book_yes": ob_yes})
    if ob_no is not None:
        tick = tick.model_copy(update={"order_book_no": ob_no})
    if pm_ts:
        tick = tick.model_copy(update={"timestamp": pm_ts})
    return MatchResult(
        pm_tick=tick,
        pm_quote=_pm_quote(yes_bid=pm_yes_bid, yes_ask=pm_yes_ask),
        options_entry=_cache_entry(bid=options_bid, ask=options_ask, ts=options_ts),
        matched_strike=100_000.0,
        matched_expiry=_EXPIRY,
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        is_interpolated=False,
    )


def _edge(
    conservative_edge: float = 0.10,
    adj_yes: float = 0.10,
    mid_yes: float = 0.15,
    match: MatchResult | None = None,
) -> EdgeResult:
    m = match or _match()
    return EdgeResult(
        match=m,
        edge_yes_mid=mid_yes,
        edge_no_mid=0.02,
        edge_yes_conservative=conservative_edge,
        edge_no_conservative=-0.02,
        adjusted_edge_yes=adj_yes,
        adjusted_edge_no=-0.02,
        best_side="buy_yes",
        best_conservative_edge=conservative_edge,
        timestamp=_NOW,
    )


# ══════════════════════════════════════════════════════════════════════════════
# FeedHealthTracker
# ══════════════════════════════════════════════════════════════════════════════

def test_feed_health_inf_when_never_ticked():
    h = FeedHealthTracker()
    assert h.staleness_ms(DataSource.DERIBIT) == float("inf")


def test_feed_health_near_zero_after_tick():
    h = FeedHealthTracker()
    h.record_tick(DataSource.DERIBIT)
    assert h.staleness_ms(DataSource.DERIBIT) < 50.0   # < 50 ms


def test_feed_health_is_stale():
    h = FeedHealthTracker()
    old_ts = _NOW - timedelta(seconds=10)
    h.record_tick(DataSource.DERIBIT, ts=old_ts)
    assert h.is_stale(DataSource.DERIBIT, max_age_s=5.0)
    assert not h.is_stale(DataSource.DERIBIT, max_age_s=60.0)


def test_feed_health_all_staleness_keys():
    h = FeedHealthTracker()
    summary = h.all_staleness_ms()
    # Keys are the lowercase enum *values* ("deribit", "polymarket", "kalshi"),
    # not str(member) — see FeedHealthTracker.all_staleness_ms docstring for
    # why (dashboard JS does feeds["deribit"] lookups, not the verbose
    # "DataSource.DERIBIT" form that str() produces on a (str, Enum) mixin).
    for src in DataSource:
        assert src.value in summary


# ══════════════════════════════════════════════════════════════════════════════
# OddsVelocityTracker
# ══════════════════════════════════════════════════════════════════════════════

def test_velocity_none_with_no_history():
    t = OddsVelocityTracker()
    assert t.velocity_at("contract-A", implied_prob=0.60) is None


def test_velocity_none_with_single_point():
    t = OddsVelocityTracker()
    t.update("contract-A", yes_price=0.40)
    assert t.velocity_at("contract-A", implied_prob=0.60) is None


def test_velocity_converging():
    """Price moving from 0.40 toward implied 0.60 → converging."""
    t = OddsVelocityTracker(window_secs=60.0)
    base = _NOW - timedelta(seconds=20)
    t.update("c", yes_price=0.40, ts=base)
    t.update("c", yes_price=0.45, ts=base + timedelta(seconds=10))
    t.update("c", yes_price=0.50, ts=base + timedelta(seconds=20))
    result = t.velocity_at("c", implied_prob=0.60, ts=base + timedelta(seconds=20))
    assert result is not None
    assert result.direction == "converging"
    assert result.velocity > 0


def test_velocity_diverging():
    """Price moving from 0.50 away from implied 0.60 (decreasing) → diverging."""
    t = OddsVelocityTracker(window_secs=60.0)
    base = _NOW - timedelta(seconds=20)
    t.update("c", yes_price=0.50, ts=base)
    t.update("c", yes_price=0.45, ts=base + timedelta(seconds=10))
    t.update("c", yes_price=0.40, ts=base + timedelta(seconds=20))
    result = t.velocity_at("c", implied_prob=0.60, ts=base + timedelta(seconds=20))
    assert result is not None
    assert result.direction == "diverging"


def test_velocity_stable_when_below_threshold():
    """Tiny price change → stable (below min_velocity)."""
    t = OddsVelocityTracker(window_secs=60.0, min_velocity=0.01)
    base = _NOW - timedelta(seconds=20)
    t.update("c", yes_price=0.500_00, ts=base)
    t.update("c", yes_price=0.500_01, ts=base + timedelta(seconds=20))
    result = t.velocity_at("c", implied_prob=0.60, ts=base + timedelta(seconds=20))
    assert result is not None
    assert result.direction == "stable"


def test_velocity_only_uses_window():
    """Observations outside window_secs are excluded."""
    t = OddsVelocityTracker(window_secs=10.0)
    old = _NOW - timedelta(seconds=30)
    t.update("c", yes_price=0.20, ts=old)       # outside window
    t.update("c", yes_price=0.40, ts=_NOW - timedelta(seconds=5))
    t.update("c", yes_price=0.42, ts=_NOW)
    result = t.velocity_at("c", implied_prob=0.60, ts=_NOW)
    assert result is not None
    assert result.n_samples == 2   # only the two recent ones


def test_velocity_clear():
    t = OddsVelocityTracker()
    t.update("c", yes_price=0.40)
    t.clear("c")
    assert t.history("c") == []


# ══════════════════════════════════════════════════════════════════════════════
# Filter — Data Freshness Gate
# ══════════════════════════════════════════════════════════════════════════════

def test_feed_freshness_passes_when_no_tracker():
    """No FeedHealthTracker → gate is skipped."""
    e = _edge()
    filt = SignalFilter()
    assert filt.explains(e, feed_health=None) is None


def test_feed_freshness_rejects_stale_deribit():
    h = FeedHealthTracker()
    h.record_tick(DataSource.DERIBIT, ts=_NOW - timedelta(seconds=10))
    h.record_tick(DataSource.KALSHI, ts=_NOW)
    e = _edge()
    filt = SignalFilter(FilterConfig(max_deribit_staleness_s=5.0))
    reason = filt.explains(e, feed_health=h)
    assert reason is not None
    assert "deribit_feed_stale" in reason


def test_feed_freshness_rejects_stale_pm():
    h = FeedHealthTracker()
    h.record_tick(DataSource.DERIBIT, ts=_NOW)
    h.record_tick(DataSource.KALSHI, ts=_NOW - timedelta(seconds=30))
    e = _edge()
    filt = SignalFilter(FilterConfig(max_pm_staleness_s=15.0))
    reason = filt.explains(e, feed_health=h)
    assert reason is not None
    assert "kalshi" in reason.lower() or "feed_stale" in reason


def test_feed_freshness_passes_fresh_feeds():
    h = FeedHealthTracker()
    h.record_tick(DataSource.DERIBIT, ts=_NOW)
    h.record_tick(DataSource.KALSHI, ts=_NOW)
    e = _edge()
    filt = SignalFilter()
    reason = filt.explains(e, feed_health=h)
    # Should pass the freshness gate (may still fail other gates)
    if reason is not None:
        assert "feed_stale" not in reason


# ══════════════════════════════════════════════════════════════════════════════
# Filter — Odds Velocity Gate
# ══════════════════════════════════════════════════════════════════════════════

def test_odds_velocity_gate_skipped_without_tracker():
    e = _edge()
    filt = SignalFilter()
    # No exception; gate returns None
    reason = filt.explains(e, odds_tracker=None)
    # May fail for other reasons but not velocity
    if reason:
        assert "odds_converging" not in reason


def test_odds_velocity_rejects_converging_above_threshold():
    tracker = OddsVelocityTracker(window_secs=60.0, min_velocity=1e-4)
    base = _NOW - timedelta(seconds=20)
    # Price moving 0.40 → 0.50 with implied = 0.58 → converging
    tracker.update("pm-btc-100000", yes_price=0.40, ts=base)
    tracker.update("pm-btc-100000", yes_price=0.50, ts=base + timedelta(seconds=20))

    e = _edge()
    filt = SignalFilter(FilterConfig(odds_velocity_threshold=1e-4))
    reason = filt.explains(e, odds_tracker=tracker)
    assert reason is not None
    assert "odds_converging" in reason


def test_odds_velocity_allows_diverging():
    tracker = OddsVelocityTracker(window_secs=60.0, min_velocity=1e-4)
    base = _NOW - timedelta(seconds=20)
    # Price moving 0.55 → 0.45 (away from implied 0.58) → diverging
    tracker.update("pm-btc-100000", yes_price=0.55, ts=base)
    tracker.update("pm-btc-100000", yes_price=0.45, ts=base + timedelta(seconds=20))

    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter(FilterConfig(odds_velocity_threshold=1e-4))
    reason = filt.explains(e, odds_tracker=tracker)
    # Should NOT be rejected for velocity (may fail other criteria)
    if reason:
        assert "odds_converging" not in reason


# ══════════════════════════════════════════════════════════════════════════════
# Filter — Volatility Regime Gate
# ══════════════════════════════════════════════════════════════════════════════

def test_vol_regime_skipped_without_tracker():
    e = _edge(conservative_edge=0.03)
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.03))
    reason = filt.explains(e, rv_tracker=None)
    if reason:
        assert "regime" not in reason


def test_vol_regime_low_uses_tighter_threshold():
    """LOW regime multiplier = 0.8 → min = 0.03 * 0.8 = 0.024.
    Edge = 0.025 → should PASS (0.025 > 0.024)."""
    rv = MagicMock()
    rv.current_regime.return_value = VolRegime.LOW
    rv.effective_min_edge.return_value = 0.024   # 0.03 * 0.8

    e = _edge(conservative_edge=0.025, adj_yes=0.025, mid_yes=0.05)
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.03, use_vol_regime_adjustment=True))
    reason = filt.explains(e, rv_tracker=rv)
    if reason:
        assert "regime_adjusted_edge" not in reason


def test_vol_regime_high_requires_wider_edge():
    """HIGH regime multiplier = 1.5 → min = 0.03 * 1.5 = 0.045.
    Edge = 0.04 → should FAIL."""
    rv = MagicMock()
    rv.current_regime.return_value = VolRegime.HIGH
    rv.effective_min_edge.return_value = 0.045   # 0.03 * 1.5

    e = _edge(conservative_edge=0.04, adj_yes=0.04, mid_yes=0.07)
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.03, use_vol_regime_adjustment=True))
    reason = filt.explains(e, rv_tracker=rv)
    assert reason is not None
    assert "regime_adjusted_edge" in reason


def test_vol_regime_disabled_via_config():
    rv = MagicMock()
    rv.effective_min_edge.return_value = 0.10   # very high, but gate disabled

    e = _edge(conservative_edge=0.04, adj_yes=0.04, mid_yes=0.07)
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.03, use_vol_regime_adjustment=False))
    reason = filt.explains(e, rv_tracker=rv)
    if reason:
        assert "regime_adjusted_edge" not in reason


# ══════════════════════════════════════════════════════════════════════════════
# Fill-adjusted edge
# ══════════════════════════════════════════════════════════════════════════════

def test_fill_adjusted_price_exact_fill():
    """Single level with enough size → returns that level's price."""
    book = [(0.44, 500.0)]
    result = fill_adjusted_price(book, size_usd=200.0)
    assert result == pytest.approx(0.44)


def test_fill_adjusted_price_walks_multiple_levels():
    """Walk across two levels: 100 @ 0.44, 200 @ 0.46.
    Fill 200 total: 100 * 0.44 + 100 * 0.46 = 90 → VWAP = 0.45."""
    book = [(0.44, 100.0), (0.46, 200.0)]
    result = fill_adjusted_price(book, size_usd=200.0)
    assert result == pytest.approx(0.45)


def test_fill_adjusted_price_returns_none_for_thin_book():
    """Book only has 100 USD but want 200 → None."""
    book = [(0.44, 100.0)]
    assert fill_adjusted_price(book, size_usd=200.0) is None


def test_fill_adjusted_price_empty_book_none():
    assert fill_adjusted_price([], size_usd=100.0) is None


def test_fill_adjusted_price_zero_size_none():
    book = [(0.44, 500.0)]
    assert fill_adjusted_price(book, size_usd=0.0) is None


def test_edge_result_has_fill_adjusted_when_book_present():
    """With an order book, EdgeResult.fill_adjusted_edge should be set."""
    ob = [(0.44, 300.0), (0.46, 300.0)]
    m = _match(options_bid=0.56, options_ask=0.60, ob_yes=ob)
    calc = EdgeCalculator()
    result = calc.compute(m)
    assert result.fill_adjusted_edge is not None
    expected = 0.56 - 0.44   # options_bid - first level price
    assert result.fill_adjusted_edge == pytest.approx(expected)


def test_edge_result_fill_adjusted_none_without_book():
    m = _match(options_bid=0.56, options_ask=0.60)
    calc = EdgeCalculator()
    result = calc.compute(m)
    assert result.fill_adjusted_edge is None


def test_fill_adjusted_edge_in_arbitrage_signal():
    """fill_adjusted_edge flows through to ArbitrageSignal via _to_arbitrage_signal."""
    from btc_pm_arb.pricing.cache import ProbabilityCache
    from btc_pm_arb.signals.matcher import ContractMatcher

    cache = ProbabilityCache()
    cache.update(
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=0.56,
        ask_prob=0.60,
        mid_prob=0.58,
        source=DataSource.DERIBIT,
        timestamp=_NOW,
    )
    # Create PM tick with order book
    tick = _pm_tick(yes_bid=0.40, yes_ask=0.44)
    tick = tick.model_copy(update={"order_book_yes": [(0.44, 500.0)]})

    matcher = ContractMatcher()
    match = matcher.match(tick, cache)
    assert match is not None

    calc = EdgeCalculator()
    edge = calc.compute(match)

    filt = SignalFilter(FilterConfig(min_conservative_edge=0.05))
    signals = filt.filter([edge])
    assert signals
    assert signals[0].fill_adjusted_edge is not None


# ══════════════════════════════════════════════════════════════════════════════
# ArbitrageSignal new fields
# ══════════════════════════════════════════════════════════════════════════════

def test_signal_has_feed_staleness_field():
    from btc_pm_arb.models import ArbitrageSignal, ProbabilityQuote
    sig = ArbitrageSignal(
        options_quote=_pm_quote(),
        pm_quote=_pm_quote(),
        raw_edge=0.10,
        adjusted_edge=0.10,
        trade_side="buy_yes",
        confidence=0.75,
        feed_staleness_ms={"deribit": 12.5, "kalshi": 30.0},
        vol_regime="normal",
        timestamp=_NOW,
    )
    assert sig.feed_staleness_ms["deribit"] == 12.5
    assert sig.vol_regime == "normal"


def test_signal_feed_staleness_defaults_empty():
    from btc_pm_arb.models import ArbitrageSignal
    sig = ArbitrageSignal(
        options_quote=_pm_quote(),
        pm_quote=_pm_quote(),
        raw_edge=0.10,
        adjusted_edge=0.10,
        trade_side="buy_yes",
        confidence=0.75,
        timestamp=_NOW,
    )
    assert sig.feed_staleness_ms == {}
    assert sig.fill_adjusted_edge is None
    assert sig.vol_regime == "normal"


def test_signal_vol_regime_flows_from_filter():
    rv = MagicMock()
    rv.current_regime.return_value = VolRegime.HIGH
    rv.effective_min_edge.return_value = 0.015   # low enough to pass

    m = _match()
    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15, match=m)
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.03))
    signals = filt.filter([e], rv_tracker=rv)
    assert signals
    assert signals[0].vol_regime == str(VolRegime.HIGH)
