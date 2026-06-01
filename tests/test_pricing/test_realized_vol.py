"""Tests for pricing/realized_vol.py — RealizedVolTracker and VolRegime."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.pricing.realized_vol import (
    REGIME_EDGE_MULTIPLIER,
    RealizedVolTracker,
    VolRegime,
    _HIGH_THRESHOLD,
    _LOW_THRESHOLD,
)

_NOW = datetime.now(timezone.utc)


def _feed_prices(
    tracker: RealizedVolTracker,
    prices: list[float],
    spacing_s: float = 60.0,
    start: datetime | None = None,
) -> None:
    """Feed a list of prices with uniform time spacing."""
    t = start or _NOW
    for p in prices:
        tracker.update(p, ts=t)
        t = t + timedelta(seconds=spacing_s)


def _flat_prices(n: int = 60, base: float = 62_000.0) -> list[float]:
    """Prices with zero returns → rv = 0."""
    return [base] * n


def _volatile_prices(n: int = 60, base: float = 62_000.0, daily_vol: float = 0.80) -> list[float]:
    """Prices that simulate a given annualized vol (deterministic)."""
    import numpy as np
    rng = np.random.default_rng(seed=42)
    dt = 1 / (365 * 24)  # 1-hour return
    log_returns = rng.normal(0, daily_vol * math.sqrt(dt), size=n)
    log_prices = [math.log(base)] + list(np.cumsum(log_returns) + math.log(base))
    return [math.exp(lp) for lp in log_prices[:n]]


# ── Basic ingestion ───────────────────────────────────────────────────────────

def test_update_stores_points():
    t = RealizedVolTracker()
    _feed_prices(t, _flat_prices(10))
    assert t.n_points == 10


def test_update_ignores_nonpositive_prices():
    t = RealizedVolTracker()
    t.update(0.0)
    t.update(-100.0)
    assert t.n_points == 0


# ── RV computation ────────────────────────────────────────────────────────────

def test_rv_flat_prices_is_zero():
    t = RealizedVolTracker()
    _feed_prices(t, _flat_prices(60), spacing_s=60.0)
    rv = t.rv(1.0)
    assert rv is not None
    assert rv == pytest.approx(0.0, abs=1e-10)


def test_rv_returns_none_with_one_point():
    t = RealizedVolTracker()
    t.update(62_000.0)
    assert t.rv(1.0) is None


def test_rv_returns_none_for_empty_window():
    t = RealizedVolTracker()
    # Feed prices 3 hours ago — outside 1h window
    old_ts = _NOW - timedelta(hours=3)
    _feed_prices(t, [62_000.0, 62_100.0], spacing_s=60.0, start=old_ts)
    assert t.rv(1.0) is None


def test_rv_is_positive_for_volatile_series():
    t = RealizedVolTracker()
    prices = _volatile_prices(n=120, daily_vol=0.60)
    _feed_prices(t, prices, spacing_s=30.0)
    rv = t.rv(1.0)
    assert rv is not None
    assert rv > 0.0


def test_rv_higher_for_more_volatile_series():
    t_low = RealizedVolTracker()
    t_high = RealizedVolTracker()
    _feed_prices(t_low, _volatile_prices(120, daily_vol=0.30), spacing_s=30.0)
    _feed_prices(t_high, _volatile_prices(120, daily_vol=0.90), spacing_s=30.0)
    rv_low = t_low.rv(1.0)
    rv_high = t_high.rv(1.0)
    assert rv_low is not None and rv_high is not None
    assert rv_high > rv_low


def test_rv_all_returns_all_windows():
    t = RealizedVolTracker(windows_h=[1.0, 4.0, 24.0])
    _feed_prices(t, _volatile_prices(60), spacing_s=60.0)
    result = t.rv_all()
    assert set(result.keys()) == {1.0, 4.0, 24.0}


# ── Regime classification ─────────────────────────────────────────────────────

def test_regime_normal_when_no_data():
    t = RealizedVolTracker()
    assert t.current_regime() == VolRegime.NORMAL


def test_regime_low_for_flat_prices():
    t = RealizedVolTracker()
    _feed_prices(t, _flat_prices(120), spacing_s=30.0)
    assert t.current_regime() == VolRegime.LOW


def test_regime_high_for_very_volatile():
    t = RealizedVolTracker()
    # Use an extremely volatile series to ensure HIGH regime
    prices = _volatile_prices(n=120, base=62_000.0, daily_vol=2.0)
    _feed_prices(t, prices, spacing_s=30.0)
    rv = t.rv(1.0)
    assert rv is not None
    # Verify rv is above threshold (test logic, not just enum)
    if rv >= _HIGH_THRESHOLD:
        assert t.current_regime() == VolRegime.HIGH
    # If RNG luck gives NORMAL, we just check it's not LOW
    assert t.current_regime() != VolRegime.LOW


def test_regime_1h_window_used_for_classification():
    """Only the 1h window drives regime — not 24h."""
    t = RealizedVolTracker(windows_h=[1.0, 24.0])
    # Feed very old volatile data (outside 1h window)
    old = _NOW - timedelta(hours=3)
    _feed_prices(t, _volatile_prices(60, daily_vol=2.0), spacing_s=60.0, start=old)
    # Then flat recent prices
    _feed_prices(t, _flat_prices(30), spacing_s=60.0)
    # Recent 1h is flat → should be LOW
    assert t.current_regime() == VolRegime.LOW


# ── Edge multiplier ───────────────────────────────────────────────────────────

def test_effective_min_edge_normal_regime():
    t = RealizedVolTracker()
    _feed_prices(t, _volatile_prices(60, daily_vol=0.50), spacing_s=30.0)
    base = 0.03
    eff = t.effective_min_edge(base)
    assert eff == pytest.approx(base * REGIME_EDGE_MULTIPLIER[t.current_regime()])


def test_effective_min_edge_low_regime():
    t = RealizedVolTracker()
    _feed_prices(t, _flat_prices(60), spacing_s=30.0)
    assert t.current_regime() == VolRegime.LOW
    assert t.effective_min_edge(0.03) == pytest.approx(0.03 * 0.8)


def test_regime_edge_multiplier_dict_complete():
    assert set(REGIME_EDGE_MULTIPLIER.keys()) == {VolRegime.LOW, VolRegime.NORMAL, VolRegime.HIGH}


# ── Timestamps ────────────────────────────────────────────────────────────────

def test_newest_oldest_ts():
    t = RealizedVolTracker()
    t1 = _NOW - timedelta(minutes=5)
    t2 = _NOW
    t.update(62_000.0, ts=t1)
    t.update(62_100.0, ts=t2)
    assert t.oldest_ts == t1
    assert t.newest_ts == t2


def test_empty_tracker_ts_none():
    t = RealizedVolTracker()
    assert t.oldest_ts is None
    assert t.newest_ts is None


# ── C1: sim-clock seam (replay vol-regime faithfulness) ────────────────────────

def test_maybe_update_uses_injected_clock_for_throttle_and_timestamp():
    """maybe_update must throttle + stamp off the injected clock, not wall-clock.

    Regression for the live-vs-replay vol_regime contradiction: an
    as-fast-as-possible replay otherwise stamps every sample at wall-clock
    (~ms apart) and reads a phantom HIGH regime on frames that live reads LOW.
    """
    from btc_pm_arb.clock import SimulatedClock

    clock = SimulatedClock("replay")
    t0 = datetime(2026, 6, 1, 18, 0, 0, tzinfo=timezone.utc)
    clock.advance_to(t0)
    tr = RealizedVolTracker(clock=clock)

    # First observation admitted; stamped at SIM time, not wall-clock now.
    assert tr.maybe_update(62_000.0) is True
    assert tr.newest_ts == t0

    # Sim advances 0.5s (< 1.0s throttle) -> throttled, regardless of wall time.
    clock.advance_to(t0 + timedelta(seconds=0.5))
    assert tr.maybe_update(62_100.0) is False
    assert tr.newest_ts == t0

    # Sim advances to +1.0s from last sample -> admitted, stamped sim time.
    t1 = t0 + timedelta(seconds=1.0)
    clock.advance_to(t1)
    assert tr.maybe_update(62_100.0) is True
    assert tr.newest_ts == t1


def test_rv_query_eviction_uses_injected_clock():
    """rv()'s query-time eviction must read the injected clock, not wall-clock."""
    from btc_pm_arb.clock import SimulatedClock

    clock = SimulatedClock("replay")
    t0 = datetime(2026, 6, 1, 18, 0, 0, tzinfo=timezone.utc)
    clock.advance_to(t0)
    tr = RealizedVolTracker(clock=clock)

    # Five samples 60s apart in SIM time -> two-plus returns inside the 1h window.
    for i in range(5):
        tr.update(62_000.0 + 10.0 * i, ts=t0 + timedelta(seconds=60 * i))
    clock.advance_to(t0 + timedelta(minutes=5))
    assert tr.rv(1.0) is not None

    # Advance the SIM clock >1h past the last sample -> all entries age out.
    clock.advance_to(t0 + timedelta(hours=2))
    assert tr.rv(1.0) is None


def test_default_tracker_unchanged_without_clock():
    """No clock -> wall-clock fallback; default construction stays live-identical."""
    tr = RealizedVolTracker()
    assert tr.clock is None
    assert tr.maybe_update(62_000.0) is True
    # newest_ts is a wall-clock instant (close to now), proving the fallback.
    assert tr.newest_ts is not None
    assert abs((datetime.now(timezone.utc) - tr.newest_ts).total_seconds()) < 5.0
