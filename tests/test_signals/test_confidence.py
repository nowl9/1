"""Tests for signals/confidence.py — ConfidenceScorer.

Tests verify:
  - All five dimensions contribute to a score in [0, 1].
  - High-quality inputs → score near 1.0.
  - Low-quality inputs → score near 0.0.
  - Neutral score (0.5) when surface is missing (vol_fit falls back to neutral).
  - dimension_breakdown() returns one key per dimension.
  - Custom ConfidenceConfig weights are honoured.
  - score_all() matches individual score() calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from btc_pm_arb.models import DataSource, PredictionMarketTick, ProbabilityQuote
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.signals.confidence import ConfidenceConfig, ConfidenceScorer
from btc_pm_arb.signals.edge import EdgeResult
from btc_pm_arb.signals.matcher import MatchResult

# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime.now(timezone.utc)
_EXPIRY = _NOW + timedelta(days=27)


def _pm_tick(yes_bid: float = 0.40, yes_ask: float = 0.44) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=DataSource.POLYMARKET,
        contract_id="pm-btc-100000",
        question="BTC above $100,000?",
        strike=100_000.0,
        expiry=_EXPIRY,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        timestamp=_NOW,
    )


def _pm_quote(yes_bid: float = 0.40, yes_ask: float = 0.44) -> ProbabilityQuote:
    return ProbabilityQuote(
        source=DataSource.POLYMARKET,
        contract_id="pm-btc-100000",
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=yes_bid,
        ask_prob=yes_ask,
        mid_prob=(yes_bid + yes_ask) / 2,
        direction="above",
        settlement_type="polymarket_spot",
        timestamp=_NOW,
    )


def _options_entry(bid: float = 0.55, ask: float = 0.57) -> CacheEntry:
    return CacheEntry(
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=bid,
        ask_prob=ask,
        mid_prob=(bid + ask) / 2,
        source=DataSource.DERIBIT,
        timestamp=_NOW,
    )


def _match_result(
    options_bid: float = 0.55,
    options_ask: float = 0.57,
    pm_yes_bid: float = 0.40,
    pm_yes_ask: float = 0.44,
    quality: float = 1.0,
) -> MatchResult:
    return MatchResult(
        pm_tick=_pm_tick(yes_bid=pm_yes_bid, yes_ask=pm_yes_ask),
        pm_quote=_pm_quote(yes_bid=pm_yes_bid, yes_ask=pm_yes_ask),
        options_entry=_options_entry(bid=options_bid, ask=options_ask),
        matched_strike=100_000.0,
        matched_expiry=_EXPIRY,
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=quality,
        is_interpolated=False,
    )


def _edge(
    options_bid: float = 0.55,
    options_ask: float = 0.57,
    pm_yes_bid: float = 0.40,
    pm_yes_ask: float = 0.44,
    quality: float = 1.0,
    persistence: float = 0.8,
) -> EdgeResult:
    m = _match_result(
        options_bid=options_bid,
        options_ask=options_ask,
        pm_yes_bid=pm_yes_bid,
        pm_yes_ask=pm_yes_ask,
        quality=quality,
    )
    conservative_edge = options_bid - pm_yes_ask
    return EdgeResult(
        match=m,
        edge_yes_mid=options_bid - (pm_yes_bid + pm_yes_ask) / 2,
        edge_no_mid=0.0,
        edge_yes_conservative=conservative_edge,
        edge_no_conservative=-0.05,
        adjusted_edge_yes=conservative_edge,
        adjusted_edge_no=-0.05,
        best_side="buy_yes",
        best_conservative_edge=max(0.0, conservative_edge),
        edge_history_mean=conservative_edge,
        edge_history_std=0.01,
        edge_persistence=persistence,
        timestamp=_NOW,
    )


def _mock_surface(rmse: float = 0.01) -> MagicMock:
    mock_smile = MagicMock()
    mock_smile.fit_rmse = rmse
    surface = MagicMock()
    surface.get_smile.return_value = mock_smile
    return surface


# ── Score range ───────────────────────────────────────────────────────────────

def test_score_in_unit_interval():
    scorer = ConfidenceScorer()
    score = scorer.score(_edge())
    assert 0.0 <= score <= 1.0


def test_high_quality_inputs_high_score():
    """Tight spreads, perfect match, high persistence → score near 1."""
    e = _edge(
        options_bid=0.555,
        options_ask=0.557,   # 0.2 % options spread
        pm_yes_bid=0.39,
        pm_yes_ask=0.41,     # 2 % PM spread
        quality=1.0,
        persistence=1.0,
    )
    surface = _mock_surface(rmse=0.001)
    score = ConfidenceScorer().score(e, surface=surface)
    assert score > 0.75


def test_low_quality_inputs_low_score():
    """Wide spreads, low match quality, zero persistence → score near 0."""
    e = _edge(
        options_bid=0.45,
        options_ask=0.65,    # 20 % options spread (at max)
        pm_yes_bid=0.30,
        pm_yes_ask=0.45,     # 15 % PM spread (at max)
        quality=0.0,
        persistence=0.0,
    )
    surface = _mock_surface(rmse=0.08)  # above vol_rmse_max
    score = ConfidenceScorer().score(e, surface=surface)
    assert score < 0.35


# ── Individual dimension tests ────────────────────────────────────────────────

def test_options_spread_dimension():
    """Options spread at zero → options_spread dimension = 1.0."""
    cfg = ConfidenceConfig(options_spread_max=0.20)
    scorer = ConfidenceScorer(cfg)
    e_tight = _edge(options_bid=0.560, options_ask=0.560)  # zero spread
    e_wide  = _edge(options_bid=0.450, options_ask=0.650)  # 20 % = max

    bd_tight = scorer.dimension_breakdown(e_tight)
    bd_wide  = scorer.dimension_breakdown(e_wide)

    assert bd_tight["options_spread"] > bd_wide["options_spread"]
    assert bd_tight["options_spread"] == pytest.approx(1.0)
    assert bd_wide["options_spread"] == pytest.approx(0.0)


def test_pm_depth_dimension():
    """Tight PM spread → high pm_depth score."""
    scorer = ConfidenceScorer()
    e_tight = _edge(pm_yes_bid=0.44, pm_yes_ask=0.44)   # zero spread
    e_wide  = _edge(pm_yes_bid=0.30, pm_yes_ask=0.45)   # 15 % spread

    bd_tight = scorer.dimension_breakdown(e_tight)
    bd_wide  = scorer.dimension_breakdown(e_wide)

    assert bd_tight["pm_depth"] > bd_wide["pm_depth"]


def test_vol_fit_dimension_with_surface():
    scorer = ConfidenceScorer()
    e = _edge()

    good_surface = _mock_surface(rmse=0.005)
    bad_surface  = _mock_surface(rmse=0.07)

    bd_good = scorer.dimension_breakdown(e, surface=good_surface)
    bd_bad  = scorer.dimension_breakdown(e, surface=bad_surface)

    assert bd_good["vol_fit"] > bd_bad["vol_fit"]


def test_vol_fit_neutral_without_surface():
    """No surface → vol_fit dimension falls back to 0.5 (neutral)."""
    scorer = ConfidenceScorer()
    e = _edge()
    bd = scorer.dimension_breakdown(e, surface=None)
    assert bd["vol_fit"] == pytest.approx(0.5)


def test_match_quality_dimension():
    scorer = ConfidenceScorer()
    e_good = _edge(quality=1.0)
    e_bad  = _edge(quality=0.0)
    bd_good = scorer.dimension_breakdown(e_good)
    bd_bad  = scorer.dimension_breakdown(e_bad)
    assert bd_good["match_quality"] == pytest.approx(1.0)
    assert bd_bad["match_quality"] == pytest.approx(0.0)


def test_edge_persistence_dimension():
    scorer = ConfidenceScorer()
    e_persistent = _edge(persistence=1.0)
    e_fleeting   = _edge(persistence=0.0)
    bd_p = scorer.dimension_breakdown(e_persistent)
    bd_f = scorer.dimension_breakdown(e_fleeting)
    assert bd_p["edge_persistence"] > bd_f["edge_persistence"]


# ── dimension_breakdown ───────────────────────────────────────────────────────

def test_dimension_breakdown_has_five_keys():
    scorer = ConfidenceScorer()
    bd = scorer.dimension_breakdown(_edge())
    assert len(bd) == 5


def test_dimension_breakdown_all_in_unit_interval():
    scorer = ConfidenceScorer()
    bd = scorer.dimension_breakdown(_edge(), surface=_mock_surface())
    for name, val in bd.items():
        assert 0.0 <= val <= 1.0, f"{name} = {val} out of [0, 1]"


def test_dimension_breakdown_keys():
    scorer = ConfidenceScorer()
    bd = scorer.dimension_breakdown(_edge())
    expected = {"options_spread", "pm_depth", "vol_fit", "match_quality", "edge_persistence"}
    assert set(bd.keys()) == expected


# ── score_all ─────────────────────────────────────────────────────────────────

def test_score_all_matches_individual_scores():
    scorer = ConfidenceScorer()
    edges = [_edge(quality=q, persistence=p) for q, p in [(1.0, 1.0), (0.5, 0.5), (0.0, 0.0)]]
    surface = _mock_surface()
    batch = scorer.score_all(edges, surface=surface)
    individual = [scorer.score(e, surface=surface) for e in edges]
    for b, i in zip(batch, individual):
        assert b == pytest.approx(i)


def test_score_all_empty():
    scorer = ConfidenceScorer()
    assert scorer.score_all([]) == []


# ── Custom config ─────────────────────────────────────────────────────────────

def test_custom_config_weights_applied():
    """Setting match_quality weight to 0 should not change score when quality varies."""
    cfg_no_mq = ConfidenceConfig(w_match_quality=0.0)
    cfg_normal = ConfidenceConfig()

    scorer_no_mq = ConfidenceScorer(cfg_no_mq)
    scorer_normal = ConfidenceScorer(cfg_normal)

    e_good_mq = _edge(quality=1.0)
    e_bad_mq  = _edge(quality=0.0)

    # With zero match_quality weight, score should be the same regardless of quality
    score_good = scorer_no_mq.score(e_good_mq)
    score_bad  = scorer_no_mq.score(e_bad_mq)
    assert score_good == pytest.approx(score_bad, abs=1e-9)

    # With normal weights, good quality should score higher
    assert scorer_normal.score(e_good_mq) > scorer_normal.score(e_bad_mq)


def test_score_floor_and_ceil_respected():
    cfg = ConfidenceConfig(score_floor=0.1, score_ceil=0.9)
    scorer = ConfidenceScorer(cfg)
    # Even with perfect inputs the score is capped at 0.9
    e = _edge(options_bid=0.560, options_ask=0.560, quality=1.0, persistence=1.0)
    score = scorer.score(e, surface=_mock_surface(rmse=0.0))
    assert score <= 0.9
    # Even with terrible inputs the score is floored at 0.1
    e_bad = _edge(options_bid=0.45, options_ask=0.65, quality=0.0, persistence=0.0)
    score_bad = scorer.score(e_bad, surface=_mock_surface(rmse=0.10))
    assert score_bad >= 0.1


def test_surface_smile_not_found_falls_back_to_neutral():
    """surface.get_smile returns None → vol_fit falls back to 0.5."""
    surface = MagicMock()
    surface.get_smile.return_value = None
    scorer = ConfidenceScorer()
    bd = scorer.dimension_breakdown(_edge(), surface=surface)
    assert bd["vol_fit"] == pytest.approx(0.5)
