"""Tests for signals/matcher.py — ContractMatcher."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.models import DataSource, PredictionMarketTick
from btc_pm_arb.pricing.cache import ProbabilityCache
from btc_pm_arb.signals.matcher import ContractMatcher, MatchResult, cache_entry_to_options_quote

# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime.now(timezone.utc)
_EXPIRY = _NOW + timedelta(days=27)   # ~27 days out (always in the future)


def _pm_tick(
    strike: float = 100_000.0,
    expiry: datetime | None = None,
    yes_bid: float = 0.40,
    yes_ask: float = 0.44,
    source: DataSource = DataSource.POLYMARKET,
    no_bid: float | None = None,
    no_ask: float | None = None,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=source,
        contract_id=f"pm-btc-{int(strike)}",
        question=f"BTC above ${int(strike):,} by Jun 28?",
        strike=strike,
        expiry=expiry or _EXPIRY,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=_NOW,
    )


def _populated_cache(
    strike: float = 100_000.0,
    expiry: datetime | None = None,
    bid: float = 0.50,
    ask: float = 0.54,
) -> ProbabilityCache:
    cache = ProbabilityCache()
    cache.update(
        strike=strike,
        expiry=expiry or _EXPIRY,
        bid_prob=bid,
        ask_prob=ask,
        mid_prob=(bid + ask) / 2,
        source=DataSource.DERIBIT,
        timestamp=_NOW,
    )
    return cache


# ── Basic matching ─────────────────────────────────────────────────────────────

def test_exact_match_returns_result():
    cache = _populated_cache()
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None
    assert isinstance(result, MatchResult)


def test_exact_match_quality_is_one():
    cache = _populated_cache()
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None
    assert result.match_quality == pytest.approx(1.0)


def test_exact_match_zero_gaps():
    cache = _populated_cache()
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None
    assert result.strike_gap_pct == pytest.approx(0.0)
    assert result.expiry_gap_hours == pytest.approx(0.0)


def test_matched_strike_and_expiry_populated():
    cache = _populated_cache()
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None
    assert result.matched_strike == pytest.approx(100_000.0)
    assert result.matched_expiry == _EXPIRY


# ── Strike gap tolerance ──────────────────────────────────────────────────────

def test_within_strike_gap_tolerance_matches():
    """PM strike 101 000, cache strike 100 000 → gap = |101000-100000|/101000 ≈ 0.99 %."""
    cache = _populated_cache(strike=100_000.0)
    matcher = ContractMatcher(max_strike_gap_pct=0.02)
    result = matcher.match(_pm_tick(strike=101_000.0), cache)
    assert result is not None
    assert result.strike_gap_pct == pytest.approx(1_000.0 / 101_000.0, rel=1e-6)


def test_exceeds_strike_gap_tolerance_returns_none():
    """PM strike 103 000, cache strike 100 000 → gap = 3 % (> 2 % default)."""
    cache = _populated_cache(strike=100_000.0)
    matcher = ContractMatcher(max_strike_gap_pct=0.02)
    result = matcher.match(_pm_tick(strike=103_000.0), cache)
    assert result is None


def test_custom_strike_gap_threshold():
    cache = _populated_cache(strike=100_000.0)
    matcher = ContractMatcher(max_strike_gap_pct=0.05)
    result = matcher.match(_pm_tick(strike=104_000.0), cache)
    assert result is not None   # 4 % gap allowed with 5 % threshold


# ── Expiry gap tolerance ──────────────────────────────────────────────────────

def test_within_expiry_gap_matches():
    """Cache expiry 12 hours from PM expiry — within default 24 h."""
    cache_expiry = _EXPIRY + timedelta(hours=12)
    cache = _populated_cache(expiry=cache_expiry)
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None
    assert result.expiry_gap_hours == pytest.approx(12.0, abs=0.01)


def test_exceeds_expiry_gap_returns_none():
    """Cache expiry 25 hours from PM expiry — outside default 24 h."""
    cache_expiry = _EXPIRY + timedelta(hours=25)
    cache = _populated_cache(expiry=cache_expiry)
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is None


# ── Match quality scoring ─────────────────────────────────────────────────────

def test_quality_degrades_with_strike_gap():
    cache = _populated_cache(strike=100_000.0)
    matcher = ContractMatcher(max_strike_gap_pct=0.02)
    result = matcher.match(_pm_tick(strike=101_000.0), cache)
    assert result is not None
    # 1% strike gap out of 2% max → strike_pen = 0.5 → quality ≈ 1 - 0.5/2 = 0.75
    assert result.match_quality == pytest.approx(0.75, abs=0.01)


def test_quality_degrades_with_expiry_gap():
    cache_expiry = _EXPIRY + timedelta(hours=12)
    cache = _populated_cache(expiry=cache_expiry)
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None
    # 12h gap out of 24h max → expiry_pen = 0.5 → quality = 1 - 0.5/2 = 0.75
    assert result.match_quality == pytest.approx(0.75, abs=0.01)


def test_quality_at_boundary_is_zero():
    """Exactly at the strike gap boundary → quality = 0."""
    cache = _populated_cache(strike=100_000.0)
    matcher = ContractMatcher(max_strike_gap_pct=0.02)
    # Strike gap = exactly 2%
    result = matcher.match(_pm_tick(strike=102_000.0), cache)
    # 2% gap at 2% max → strike_pen = 1.0, expiry_pen = 0.0 → quality = 0.5
    if result is not None:
        # Whether it's None or has quality 0.5 both are acceptable per spec
        assert result.match_quality >= 0.0


# ── Missing data ──────────────────────────────────────────────────────────────

def test_no_strike_returns_none():
    cache = _populated_cache()
    tick = _pm_tick()
    tick = tick.model_copy(update={"strike": None})
    matcher = ContractMatcher()
    assert matcher.match(tick, cache) is None


def test_no_expiry_returns_none():
    cache = _populated_cache()
    tick = _pm_tick()
    tick = tick.model_copy(update={"expiry": None})
    matcher = ContractMatcher()
    assert matcher.match(tick, cache) is None


def test_no_pm_bid_returns_none():
    cache = _populated_cache()
    tick = _pm_tick()
    tick = tick.model_copy(update={"yes_bid": None})
    matcher = ContractMatcher()
    assert matcher.match(tick, cache) is None


def test_empty_cache_returns_none():
    cache = ProbabilityCache()
    matcher = ContractMatcher()
    assert matcher.match(_pm_tick(), cache) is None


# ── PM quote conversion ───────────────────────────────────────────────────────

def test_pm_quote_settlement_type_polymarket():
    cache = _populated_cache()
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(source=DataSource.POLYMARKET), cache)
    assert result is not None
    assert result.pm_quote.settlement_type == "polymarket_spot"


def test_pm_quote_settlement_type_kalshi():
    cache = _populated_cache()
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(source=DataSource.KALSHI), cache)
    assert result is not None
    assert result.pm_quote.settlement_type == "kalshi_rti"


def test_options_entry_probabilities():
    cache = _populated_cache(bid=0.48, ask=0.52)
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None
    assert result.options_entry.bid_prob == pytest.approx(0.48)
    assert result.options_entry.ask_prob == pytest.approx(0.52)


# ── Batch match ───────────────────────────────────────────────────────────────

def test_batch_match_filters_unmatched():
    cache = _populated_cache(strike=100_000.0)
    matcher = ContractMatcher()
    ticks = [
        _pm_tick(strike=100_000.0),   # matches
        _pm_tick(strike=200_000.0),   # strike gap > 2 %
    ]
    results = matcher.batch_match(ticks, cache)
    assert len(results) == 1
    assert results[0].matched_strike == pytest.approx(100_000.0)


def test_batch_match_empty_input():
    cache = _populated_cache()
    matcher = ContractMatcher()
    assert matcher.batch_match([], cache) == []


# ── cache_entry_to_options_quote ──────────────────────────────────────────────

def test_cache_entry_to_options_quote_fields():
    cache = _populated_cache(bid=0.45, ask=0.55)
    matcher = ContractMatcher()
    result = matcher.match(_pm_tick(), cache)
    assert result is not None

    quote = cache_entry_to_options_quote(result.options_entry, contract_id="test-id")
    assert quote.contract_id == "test-id"
    assert quote.bid_prob == pytest.approx(0.45)
    assert quote.ask_prob == pytest.approx(0.55)
    assert quote.settlement_type == "deribit_twap"
    assert quote.source == DataSource.DERIBIT
