"""Tests for signals/filters.py — SignalFilter.

Key scenarios:
  - Clear arbitrage (10 %+ edge) passes all criteria → ArbitrageSignal emitted.
  - Marginal edge (< 3 % conservative threshold) is rejected.
  - Each individual criterion can be triggered in isolation.
  - Stale data (options or PM older than max_data_age_seconds) is rejected.
  - PM spread too wide → rejected.
  - Vol fit RMSE too high → rejected when surface provided.
  - Passing signals are sorted by adjusted_edge descending.
  - explains() returns None for passing edges and a reason string for rejected ones.

Determinism (2026-06-10): every test here is hermetic against wall-clock
drift -- _NOW is a pinned literal and the _pin_filter_clock fixture routes
SignalFilter's clock seam through it.  See the notes at both definitions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from btc_pm_arb.models import DataSource, PredictionMarketTick, ProbabilityQuote
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.signals.edge import EdgeResult
from btc_pm_arb.signals.filters import FilterConfig, SignalFilter
from btc_pm_arb.signals.matcher import MatchResult

# ── Helpers ───────────────────────────────────────────────────────────────────

# Pinned to a FIXED instant -- deliberately NOT datetime.now().  The previous
# import-time `_NOW = datetime.now(timezone.utc)` was captured at pytest
# COLLECTION (session t=0) while SignalFilter's staleness gate compared the
# fixture timestamps to the wall clock at EXECUTION time, minutes later: on
# full-suite runs slower than ~315 s, fixture age crossed the 300 s
# max_data_age_seconds cutoff and test_fresh_data_passes (30 s fixture, the
# tightest margin here) flaked with "options_data_age 301s > max 300.0s".
# The _pin_filter_clock fixture below injects this same instant into the
# filter's clock seam, so every age and days-to-expiry is exactly what the
# test names claim, regardless of suite duration or machine load.  The date
# is deliberately in the past: if a real wall-clock comparison ever sneaks
# back into these tests, they fail loudly (age ~days), not flakily.
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=27)   # ~27 days out (relative to _NOW)


@pytest.fixture(autouse=True)
def _pin_filter_clock(monkeypatch):
    """Freeze SignalFilter's wall clock at _NOW for every test in this module.

    The fixture helpers below stamp tick/entry timestamps relative to _NOW;
    production's _reject_stale_data and _reject_expiry_bounds compare those
    stamps to the per-call ``clock=`` seam (filters._ctx_now), which falls
    back to the real wall clock when unset.  Defaulting the seam to _NOW
    makes the whole module hermetic.  A test can still override by passing
    its own ``clock=`` explicitly (setdefault leaves explicit values alone).
    """
    orig_filter = SignalFilter.filter
    orig_explains = SignalFilter.explains

    def _filter_pinned(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: _NOW)
        return orig_filter(self, *args, **kwargs)

    def _explains_pinned(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: _NOW)
        return orig_explains(self, *args, **kwargs)

    monkeypatch.setattr(SignalFilter, "filter", _filter_pinned)
    monkeypatch.setattr(SignalFilter, "explains", _explains_pinned)


def _pm_tick(
    strike: float = 100_000.0,
    yes_bid: float = 0.40,
    yes_ask: float = 0.44,
    ts: datetime | None = None,
    expiry: datetime | None = None,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=DataSource.POLYMARKET,
        contract_id=f"pm-btc-{int(strike)}",
        question=f"BTC above ${int(strike):,}?",
        strike=strike,
        expiry=expiry or _EXPIRY,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        timestamp=ts or _NOW,
    )


def _pm_quote(yes_bid: float = 0.40, yes_ask: float = 0.44, ts: datetime | None = None) -> ProbabilityQuote:
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
        timestamp=ts or _NOW,
    )


def _options_entry(
    bid: float = 0.55,
    ask: float = 0.59,
    ts: datetime | None = None,
) -> CacheEntry:
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
    options_bid: float = 0.55,
    options_ask: float = 0.59,
    pm_yes_bid: float = 0.40,
    pm_yes_ask: float = 0.44,
    quality: float = 1.0,
    options_ts: datetime | None = None,
    pm_ts: datetime | None = None,
    expiry: datetime | None = None,
) -> MatchResult:
    pm = _pm_tick(
        yes_bid=pm_yes_bid,
        yes_ask=pm_yes_ask,
        ts=pm_ts,
        expiry=expiry,
    )
    return MatchResult(
        pm_tick=pm,
        pm_quote=_pm_quote(yes_bid=pm_yes_bid, yes_ask=pm_yes_ask, ts=pm_ts),
        options_entry=_options_entry(bid=options_bid, ask=options_ask, ts=options_ts),
        matched_strike=100_000.0,
        matched_expiry=expiry or _EXPIRY,
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=quality,
        is_interpolated=False,
    )


def _edge(
    match: MatchResult | None = None,
    best_side: str = "buy_yes",
    conservative_edge: float = 0.11,
    adj_yes: float = 0.11,
    adj_no: float = -0.02,
    mid_yes: float = 0.15,
    mid_no: float = 0.05,
) -> EdgeResult:
    m = match or _match()
    return EdgeResult(
        match=m,
        edge_yes_mid=mid_yes,
        edge_no_mid=mid_no,
        edge_yes_conservative=conservative_edge,
        edge_no_conservative=adj_no,
        adjusted_edge_yes=adj_yes,
        adjusted_edge_no=adj_no,
        best_side=best_side,  # type: ignore[arg-type]
        best_conservative_edge=conservative_edge,
        edge_history_mean=conservative_edge,
        edge_history_std=0.01,
        edge_persistence=0.8,
        timestamp=_NOW,
    )


# ── Clear arbitrage passes ────────────────────────────────────────────────────

def test_clear_arb_passes_filter():
    """10 %+ edge should survive all default criteria."""
    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    signals = filt.filter([e])
    assert len(signals) == 1


def test_clear_arb_signal_fields():
    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15, best_side="buy_yes")
    filt = SignalFilter()
    signals = filt.filter([e])
    sig = signals[0]
    assert sig.trade_side == "buy_yes"
    assert sig.adjusted_edge == pytest.approx(0.10)
    assert 0.0 <= sig.confidence <= 1.0


# ── Marginal edge filtered out ────────────────────────────────────────────────

def test_marginal_edge_below_threshold_rejected():
    """0.5 % conservative edge < 1 % default threshold (Round 9a) → rejected."""
    e = _edge(conservative_edge=0.005, adj_yes=0.005, mid_yes=0.02)
    filt = SignalFilter()
    signals = filt.filter([e])
    assert len(signals) == 0


def test_explains_gives_reason_for_marginal_edge():
    e = _edge(conservative_edge=0.005, adj_yes=0.005, mid_yes=0.02)
    filt = SignalFilter()
    reason = filt.explains(e)
    assert reason is not None
    assert "conservative_edge" in reason


def test_explains_returns_none_for_passing_edge():
    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert filt.explains(e) is None


# ── No positive edge ──────────────────────────────────────────────────────────

def test_no_positive_edge_rejected():
    e = _edge(best_side=None, conservative_edge=0.0)  # type: ignore[arg-type]
    e = EdgeResult(
        match=_match(),
        edge_yes_mid=-0.05,
        edge_no_mid=-0.03,
        edge_yes_conservative=-0.05,
        edge_no_conservative=-0.03,
        adjusted_edge_yes=-0.05,
        adjusted_edge_no=-0.03,
        best_side=None,
        best_conservative_edge=0.0,
        timestamp=_NOW,
    )
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "no_positive_edge" in filt.explains(e)


# ── Mid edge criterion ────────────────────────────────────────────────────────

def test_low_mid_edge_rejected():
    """Conservative edge passes but mid edge is below min_mid_edge."""
    e = _edge(conservative_edge=0.05, adj_yes=0.05, mid_yes=0.005)
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.03, min_mid_edge=0.01))
    assert filt.filter([e]) == []


def test_mid_edge_exactly_at_threshold_passes():
    e = _edge(conservative_edge=0.05, adj_yes=0.05, mid_yes=0.01)
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.03, min_mid_edge=0.01))
    assert len(filt.filter([e])) == 1


# ── Expiry bounds ─────────────────────────────────────────────────────────────

def test_near_expiry_rejected(monkeypatch):
    """Contract expiring in 12 hours should be rejected (min 1 day)."""
    soon = _NOW + timedelta(hours=12)
    m = _match(expiry=soon)
    e = _edge(match=m)
    filt = SignalFilter()
    signals = filt.filter([e])
    assert len(signals) == 0
    assert "days_to_expiry" in filt.explains(e)


def test_far_expiry_rejected():
    """Contract expiring in 120 days > 90 day max → rejected."""
    far = _NOW + timedelta(days=120)
    m = _match(expiry=far)
    e = _edge(match=m)
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "days_to_expiry" in filt.explains(e)


def test_acceptable_expiry_passes():
    good = _NOW + timedelta(days=14)
    m = _match(expiry=good)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


# ── PM spread ─────────────────────────────────────────────────────────────────

def test_wide_pm_spread_rejected():
    """Yes spread of 20 % > 12 % max → rejected."""
    m = _match(pm_yes_bid=0.30, pm_yes_ask=0.50)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "pm_spread" in filt.explains(e)


def test_tight_pm_spread_passes():
    m = _match(pm_yes_bid=0.42, pm_yes_ask=0.44)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


# ── Match quality ─────────────────────────────────────────────────────────────

def test_low_match_quality_rejected():
    m = _match(quality=0.20)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "match_quality" in filt.explains(e)


def test_acceptable_match_quality_passes():
    m = _match(quality=0.80)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


# ── Stale data ────────────────────────────────────────────────────────────────

def test_stale_options_data_rejected():
    """Options data 10 minutes old → rejected."""
    stale_ts = _NOW - timedelta(seconds=600)
    m = _match(options_ts=stale_ts)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "options_data_age" in filt.explains(e)


def test_stale_pm_data_rejected():
    """PM data 10 minutes old → rejected."""
    stale_ts = _NOW - timedelta(seconds=600)
    m = _match(pm_ts=stale_ts)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "pm_data_age" in filt.explains(e)


def test_fresh_data_passes():
    fresh_ts = _NOW - timedelta(seconds=30)
    m = _match(options_ts=fresh_ts, pm_ts=fresh_ts)
    e = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


# ── Vol fit quality ───────────────────────────────────────────────────────────

def test_high_vol_rmse_rejected_with_surface():
    """RMSE 8 % > 5 % threshold → rejected when surface provided."""
    mock_smile = MagicMock()
    mock_smile.fit_rmse = 0.08

    mock_surface = MagicMock()
    mock_surface.get_smile.return_value = mock_smile

    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert filt.filter([e], surface=mock_surface) == []
    assert "vol_rmse" in filt.explains(e, surface=mock_surface)


def test_good_vol_rmse_passes_with_surface():
    mock_smile = MagicMock()
    mock_smile.fit_rmse = 0.02

    mock_surface = MagicMock()
    mock_surface.get_smile.return_value = mock_smile

    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert len(filt.filter([e], surface=mock_surface)) == 1


def test_no_surface_skips_vol_fit_criterion():
    """Without a surface, vol-fit criterion is skipped (don't penalise missing data)."""
    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    assert len(filt.filter([e], surface=None)) == 1


# ── Ranking ───────────────────────────────────────────────────────────────────

def test_signals_ranked_by_adjusted_edge():
    e1 = _edge(conservative_edge=0.05, adj_yes=0.05, mid_yes=0.10)
    e2 = _edge(conservative_edge=0.12, adj_yes=0.12, mid_yes=0.20)
    e3 = _edge(conservative_edge=0.08, adj_yes=0.08, mid_yes=0.12)
    filt = SignalFilter()
    signals = filt.filter([e1, e2, e3])
    edges = [s.adjusted_edge for s in signals]
    assert edges == sorted(edges, reverse=True)


def test_multiple_signals_all_pass():
    edges = [
        _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15),
        _edge(conservative_edge=0.07, adj_yes=0.07, mid_yes=0.12),
        _edge(conservative_edge=0.04, adj_yes=0.04, mid_yes=0.08),
    ]
    filt = SignalFilter()
    signals = filt.filter(edges)
    assert len(signals) == 3


def test_mixed_batch_some_pass_some_fail():
    good = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    # Round 9a: bad edge updated from 0.01 → 0.005 to remain below the
    # new 1 % default conservative-edge floor.
    bad = _edge(conservative_edge=0.005, adj_yes=0.005, mid_yes=0.02)
    filt = SignalFilter()
    signals = filt.filter([good, bad])
    assert len(signals) == 1


# ── Empty input ───────────────────────────────────────────────────────────────

def test_empty_input_returns_empty():
    filt = SignalFilter()
    assert filt.filter([]) == []


# ── Round 9a: data-collection-floor defaults ──────────────────────────────────

def test_default_thresholds_are_round9a_floors():
    """Default FilterConfig matches the Round 9a pipeline-noise floor.

    Locks in the data-collection thresholds so a future tweak that
    moves them without explicit recalibration trips the test rather
    than silently changing the dataset shape.
    """
    cfg = FilterConfig()
    assert cfg.min_conservative_edge == 0.01
    assert cfg.min_mid_edge == 0.005


def test_one_percent_edge_passes_default():
    """1.1 % conservative edge passes the new default 1 % floor."""
    e = _edge(conservative_edge=0.011, adj_yes=0.011, mid_yes=0.012)
    filt = SignalFilter()
    signals = filt.filter([e])
    assert len(signals) == 1


def test_edge_at_old_three_percent_threshold_still_passes():
    """A 3 % edge — formerly at the floor — remains a clear pass.

    Sanity check that the lowering is purely additive: anything that
    used to pass at 3 % still passes at 1 %.  Important because tests
    elsewhere that explicitly construct ``FilterConfig(min_conservative_edge=0.03)``
    are unaffected by this change.
    """
    e = _edge(conservative_edge=0.03, adj_yes=0.03, mid_yes=0.05)
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


# ── Round 9a: rejection counter telemetry ─────────────────────────────────────

def test_rejection_counts_increments_on_filter():
    """Rejecting edges increment rejection_counts by reason bucket key."""
    bad1 = _edge(conservative_edge=0.005, adj_yes=0.005, mid_yes=0.02)
    bad2 = _edge(conservative_edge=0.003, adj_yes=0.003, mid_yes=0.02)
    filt = SignalFilter()
    filt.filter([bad1, bad2])
    assert filt.rejection_counts.get("conservative_edge") == 2


def test_rejection_counts_buckets_separate_reasons():
    """Different rejection reasons end up in different bucket keys."""
    edge_below = _edge(conservative_edge=0.005, adj_yes=0.005, mid_yes=0.02)
    m = _match(pm_yes_bid=0.30, pm_yes_ask=0.50)   # 20% spread > 12% max
    edge_wide = _edge(match=m, conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    filt.filter([edge_below, edge_wide])
    assert filt.rejection_counts.get("conservative_edge") == 1
    assert filt.rejection_counts.get("pm_spread") == 1


def test_rejection_counts_starts_empty():
    """Fresh SignalFilter has an empty counter — no implicit pre-population."""
    filt = SignalFilter()
    assert filt.rejection_counts == {}


def test_explains_does_not_increment_rejection_counts():
    """explains() is diagnostic — must NOT touch the cumulative counter.

    Round 9a invariant: the scan pipeline calls filter() and then
    explains() on the rejected set to decorate dashboard payloads;
    if explains() also incremented, every rejected edge would be
    counted twice.  This test locks the no-increment guarantee.
    """
    bad = _edge(conservative_edge=0.005, adj_yes=0.005, mid_yes=0.02)
    filt = SignalFilter()
    reason = filt.explains(bad)
    assert reason is not None
    assert filt.rejection_counts == {}


def test_passing_signals_do_not_increment_rejection_counts():
    """A purely-passing batch leaves the counter empty."""
    e = _edge(conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    filt.filter([e])
    assert filt.rejection_counts == {}


# ── One-touch barrier gate (polarity / barrier fix) ───────────────────────────

def _edge_with_product_type(product_type: str) -> EdgeResult:
    """Clear-edge EdgeResult whose pm_quote carries the given product_type."""
    m = _match()
    m.pm_quote = m.pm_quote.model_copy(update={"product_type": product_type})
    return _edge(match=m, conservative_edge=0.93, adj_yes=0.93, mid_yes=0.95)


def test_one_touch_barrier_rejected():
    """A one-touch barrier signal is skipped even with a huge (phantom) edge."""
    e = _edge_with_product_type("one_touch")
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "one_touch_barrier" in filt.explains(e)


def test_one_touch_rejection_counted():
    e = _edge_with_product_type("one_touch")
    filt = SignalFilter()
    filt.filter([e])
    assert filt.rejection_counts.get("one_touch_barrier") == 1


def test_terminal_product_not_rejected_by_one_touch_gate():
    """A genuine terminal contract with the same edge still passes."""
    e = _edge_with_product_type("terminal")
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


# ── Range (band) product gate ─────────────────────────────────────────────────

def test_range_product_rejected():
    """A range / band product is skipped even with a huge (phantom) edge."""
    e = _edge_with_product_type("range")
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "range_product" in filt.explains(e)


def test_range_rejection_counted():
    e = _edge_with_product_type("range")
    filt = SignalFilter()
    filt.filter([e])
    assert filt.rejection_counts.get("range_product") == 1


# ── Depth gate (empty crossed book) ───────────────────────────────────────────

def _edge_with_book(order_book_yes, order_book_no=None) -> EdgeResult:
    """Clear buy_yes EdgeResult whose pm_quote carries the given books."""
    m = _match()
    m.pm_quote = m.pm_quote.model_copy(update={
        "order_book_yes": order_book_yes,
        "order_book_no": order_book_no,
    })
    return _edge(match=m, best_side="buy_yes",
                 conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)


def test_empty_crossed_book_rejected():
    """buy_yes signal with an explicitly empty YES book is filtered."""
    e = _edge_with_book(order_book_yes=[])
    filt = SignalFilter()
    assert filt.filter([e]) == []
    assert "empty_book" in filt.explains(e)


def test_nonempty_crossed_book_passes():
    e = _edge_with_book(order_book_yes=[(0.44, 500.0)])
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


def test_none_book_skips_depth_gate():
    """Unknown depth (None) is not penalised — gate skipped."""
    e = _edge_with_book(order_book_yes=None)
    filt = SignalFilter()
    assert len(filt.filter([e])) == 1


def test_depth_gate_disabled_by_config():
    e = _edge_with_book(order_book_yes=[])
    filt = SignalFilter(FilterConfig(require_nonempty_book=False))
    assert len(filt.filter([e])) == 1


# ── Clock seam ────────────────────────────────────────────────────────────────


def test_ctx_now_falls_back_to_wall_clock():
    """The seam's default branch (no injected clock) reads the real wall
    clock.  Kept under explicit test because every other test in this
    module now pins the seam via _pin_filter_clock, so nothing else
    exercises the fallback."""
    from btc_pm_arb.signals.filters import _ctx_now

    before = datetime.now(timezone.utc)
    got = _ctx_now({})
    after = datetime.now(timezone.utc)
    assert before <= got <= after


def test_pin_filter_clock_respects_explicit_clock():
    """The autouse pin uses setdefault: a test that passes its own clock=
    still wins.  Guards the fixture's escape hatch."""
    e = _edge(match=_match(), conservative_edge=0.10, adj_yes=0.10, mid_yes=0.15)
    filt = SignalFilter()
    # With the pinned clock (age 0) the edge passes...
    assert len(filt.filter([e])) == 1
    # ...but an explicit clock 600 s ahead makes the same fixture stale.
    ahead = lambda: _NOW + timedelta(seconds=600)  # noqa: E731
    assert filt.filter([e], clock=ahead) == []
    assert "options_data_age" in filt.explains(e, clock=ahead)
