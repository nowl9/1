"""Tests for cache.py: ProbabilityCache exact lookup and interpolation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.models import DataSource, ProbabilityQuote
from btc_pm_arb.pricing.cache import CacheEntry, ProbabilityCache


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expiry(days: int) -> datetime:
    return datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc) + timedelta(days=days)


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _fill_cache(cache: ProbabilityCache) -> None:
    """Populate with a 3×3 grid: strikes 60/62/64k, expiries 30/60/90d."""
    for days in [30, 60, 90]:
        exp = _expiry(days)
        for i, strike in enumerate([60_000.0, 62_000.0, 64_000.0]):
            mid = 0.40 + i * 0.05 + days * 0.001
            cache.update(
                strike=strike, expiry=exp,
                bid_prob=mid - 0.03, ask_prob=mid + 0.03, mid_prob=mid,
                source=DataSource.DERIBIT,
            )


# ── Basic CRUD ────────────────────────────────────────────────────────────────

class TestCacheCRUD:
    def test_update_and_exact_get(self) -> None:
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, 0.44, 0.56, 0.50, DataSource.DERIBIT)
        entry = cache.get(60_000.0, exp)
        assert entry is not None
        assert entry.mid_prob == pytest.approx(0.50)
        assert entry.bid_prob == pytest.approx(0.44)
        assert entry.ask_prob == pytest.approx(0.56)

    def test_overwrite_updates_entry(self) -> None:
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, 0.44, 0.56, 0.50, DataSource.DERIBIT)
        cache.update(60_000.0, exp, 0.49, 0.51, 0.50, DataSource.DERIBIT)
        entry = cache.get(60_000.0, exp)
        assert entry is not None
        assert entry.bid_prob == pytest.approx(0.49)
        assert entry.ask_prob == pytest.approx(0.51)

    def test_get_returns_none_on_miss(self) -> None:
        cache = ProbabilityCache()
        assert cache.get(60_000.0, _expiry(30)) is None

    def test_len(self) -> None:
        cache = ProbabilityCache()
        _fill_cache(cache)
        assert len(cache) == 9

    def test_clear(self) -> None:
        cache = ProbabilityCache()
        _fill_cache(cache)
        cache.clear()
        assert len(cache) == 0
        assert cache.get(60_000.0, _expiry(30)) is None

    def test_update_from_quote(self) -> None:
        cache = ProbabilityCache()
        exp = _expiry(30)
        quote = ProbabilityQuote(
            source=DataSource.DERIBIT,
            contract_id="BTC-TEST",
            strike=60_000.0,
            expiry=exp,
            bid_prob=0.44,
            ask_prob=0.56,
            mid_prob=0.50,
            timestamp=_ts(),
        )
        cache.update_from_quote(quote)
        entry = cache.get(60_000.0, exp)
        assert entry is not None
        assert entry.mid_prob == pytest.approx(0.50)

    def test_probs_clipped_to_unit_interval(self) -> None:
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, -0.1, 1.2, 0.5, DataSource.DERIBIT)
        entry = cache.get(60_000.0, exp)
        assert entry is not None
        assert entry.bid_prob == pytest.approx(0.0)
        assert entry.ask_prob == pytest.approx(1.0)

    def test_spread_property(self) -> None:
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, 0.44, 0.56, 0.50, DataSource.DERIBIT)
        entry = cache.get(60_000.0, exp)
        assert entry is not None
        assert entry.spread == pytest.approx(0.12)

    def test_all_expiries_sorted(self) -> None:
        cache = ProbabilityCache()
        _fill_cache(cache)
        expiries = cache.all_expiries()
        assert expiries == sorted(expiries)
        assert len(expiries) == 3

    def test_entries_for_expiry(self) -> None:
        cache = ProbabilityCache()
        _fill_cache(cache)
        pairs = cache.entries_for_expiry(_expiry(30))
        strikes = [s for s, _ in pairs]
        assert strikes == sorted(strikes)
        assert len(strikes) == 3

    def test_all_strikes_for_expiry(self) -> None:
        cache = ProbabilityCache()
        _fill_cache(cache)
        strikes = cache.all_strikes_for_expiry(_expiry(30))
        assert strikes == sorted(strikes)


# ── Strike interpolation ──────────────────────────────────────────────────────

class TestStrikeInterpolation:
    def test_exact_hit_returns_stored_value(self) -> None:
        cache = ProbabilityCache()
        _fill_cache(cache)
        exp = _expiry(30)
        result = cache.interpolate(60_000.0, exp)
        stored = cache.get(60_000.0, exp)
        assert result is not None and stored is not None
        assert result.mid_prob == pytest.approx(stored.mid_prob)

    def test_interpolates_between_strikes(self) -> None:
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, 0.44, 0.56, 0.50, DataSource.DERIBIT)
        cache.update(64_000.0, exp, 0.34, 0.46, 0.40, DataSource.DERIBIT)

        result = cache.interpolate(62_000.0, exp)  # midpoint
        assert result is not None
        # Linear interpolation at midpoint → 0.45
        assert result.mid_prob == pytest.approx(0.45, abs=1e-6)

    def test_extrapolates_to_lowest_strike(self) -> None:
        """Below the lowest grid strike → returns the lowest entry."""
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, 0.44, 0.56, 0.50, DataSource.DERIBIT)
        cache.update(64_000.0, exp, 0.34, 0.46, 0.40, DataSource.DERIBIT)

        result = cache.interpolate(55_000.0, exp)
        assert result is not None
        assert result.mid_prob == pytest.approx(0.50)

    def test_extrapolates_to_highest_strike(self) -> None:
        """Above the highest grid strike → returns the highest entry."""
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, 0.44, 0.56, 0.50, DataSource.DERIBIT)
        cache.update(64_000.0, exp, 0.34, 0.46, 0.40, DataSource.DERIBIT)

        result = cache.interpolate(80_000.0, exp)
        assert result is not None
        assert result.mid_prob == pytest.approx(0.40)

    def test_interpolated_bid_le_ask(self) -> None:
        cache = ProbabilityCache()
        exp = _expiry(30)
        cache.update(60_000.0, exp, 0.40, 0.60, 0.50, DataSource.DERIBIT)
        cache.update(64_000.0, exp, 0.30, 0.50, 0.40, DataSource.DERIBIT)

        result = cache.interpolate(62_000.0, exp)
        assert result is not None
        assert result.bid_prob <= result.mid_prob <= result.ask_prob

    def test_returns_none_for_empty_cache(self) -> None:
        cache = ProbabilityCache()
        assert cache.interpolate(62_000.0, _expiry(30)) is None


# ── Expiry interpolation ──────────────────────────────────────────────────────

class TestExpiryInterpolation:
    def test_interpolates_between_expiries(self) -> None:
        cache = ProbabilityCache()
        exp30 = _expiry(30)
        exp90 = _expiry(90)
        mid_exp = _expiry(60)  # between exp30 and exp90

        cache.update(62_000.0, exp30, 0.44, 0.56, 0.50, DataSource.DERIBIT)
        cache.update(62_000.0, exp90, 0.34, 0.46, 0.40, DataSource.DERIBIT)

        result = cache.interpolate(62_000.0, mid_exp)
        assert result is not None
        # Should be between 0.40 and 0.50
        assert 0.40 <= result.mid_prob <= 0.50

    def test_uses_nearest_when_only_one_expiry(self) -> None:
        cache = ProbabilityCache()
        exp30 = _expiry(30)
        cache.update(62_000.0, exp30, 0.44, 0.56, 0.50, DataSource.DERIBIT)

        result = cache.interpolate(62_000.0, _expiry(45))
        assert result is not None
        assert result.mid_prob == pytest.approx(0.50)

    def test_full_2d_interpolation(self) -> None:
        """Interpolate at a (strike, expiry) that doesn't exist in the grid."""
        cache = ProbabilityCache()
        _fill_cache(cache)

        # Query between grid strikes and between grid expiries
        mid_strike = 61_000.0    # between 60k and 62k
        mid_exp = _expiry(45)    # between 30d and 60d expiries

        result = cache.interpolate(mid_strike, mid_exp)
        assert result is not None
        assert 0.0 <= result.mid_prob <= 1.0
        assert result.bid_prob <= result.mid_prob <= result.ask_prob

    def test_no_interp_expiries_uses_nearest(self) -> None:
        cache = ProbabilityCache()
        cache.update(62_000.0, _expiry(30), 0.44, 0.56, 0.50, DataSource.DERIBIT)
        cache.update(62_000.0, _expiry(90), 0.34, 0.46, 0.40, DataSource.DERIBIT)

        result = cache.interpolate(62_000.0, _expiry(45), interp_expiries=False)
        assert result is not None
        # Should snap to nearest expiry (30d is closer than 90d)
        assert result.mid_prob == pytest.approx(0.50)
