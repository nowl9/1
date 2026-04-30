"""Tests for digital_pricer.py: BS primitives and both pricing methods."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from scipy.stats import norm

from btc_pm_arb.models import OptionTick, OptionType
from btc_pm_arb.pricing.digital_pricer import (
    DigitalPrice,
    DigitalPricer,
    bs_call_price,
    bs_d1,
    bs_d2,
    bs_digital_price,
)
from btc_pm_arb.pricing.vol_surface import SVIParams, VolSurface


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expiry(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def _make_call_tick(
    strike: float,
    expiry: datetime,
    forward: float,
    sigma: float,
    T: float,
) -> OptionTick:
    """Synthetic call tick with BS mid price and ±2% IV half-spread."""
    mid_frac = bs_call_price(forward, strike, sigma, T) / forward
    bid_frac = bs_call_price(forward, strike, max(sigma - 0.02, 1e-4), T) / forward
    ask_frac = bs_call_price(forward, strike, sigma + 0.02, T) / forward
    return OptionTick(
        instrument_name=f"BTC-TEST-{int(strike)}-C",
        strike=strike,
        expiry=expiry,
        option_type=OptionType.CALL,
        bid=float(bid_frac),
        ask=float(ask_frac),
        mark_price=float(mid_frac),
        mark_iv=sigma * 100.0,
        underlying_price=forward,
        index_price=forward,
        timestamp=datetime.now(timezone.utc),
    )


def _surface_from_svi(
    params: SVIParams, forward: float, expiry: datetime, T: float
) -> VolSurface:
    """Build a VolSurface from SVI params with synthetic ticks."""
    strikes = [40_000, 48_000, 54_000, 58_000, 62_000, 64_000,
               68_000, 72_000, 78_000, 85_000, 92_000]
    ticks = []
    for K in strikes:
        k = np.log(K / forward)
        w = float(params.total_var(np.array([k]))[0])
        iv_pct = math.sqrt(max(w / T, 0.0)) * 100.0
        ticks.append(OptionTick(
            instrument_name=f"BTC-TEST-{int(K)}-C",
            strike=K, expiry=expiry, option_type=OptionType.CALL,
            mark_price=0.01, mark_iv=iv_pct,
            underlying_price=forward, index_price=forward,
            timestamp=datetime.now(timezone.utc),
        ))

    surface = VolSurface()
    surface.update(ticks)
    return surface


# ── BS primitives ─────────────────────────────────────────────────────────────

class TestBSPrimitives:
    def test_d1_d2_relationship(self) -> None:
        F, K, sigma, T = 62_000.0, 62_000.0, 0.65, 30 / 365
        d1 = bs_d1(F, K, sigma, T)
        d2 = bs_d2(F, K, sigma, T)
        assert d1 - d2 == pytest.approx(sigma * math.sqrt(T), rel=1e-6)

    def test_atm_d2(self) -> None:
        """ATM d2 = -0.5*σ*√T."""
        F, K, sigma, T = 62_000.0, 62_000.0, 0.60, 30 / 365
        expected = -0.5 * sigma * math.sqrt(T)
        assert bs_d2(F, K, sigma, T) == pytest.approx(expected, rel=1e-6)

    def test_call_price_atm_approximation(self) -> None:
        """ATM BS call ≈ F·σ·√(T/2π) (Bachelier ATM approximation)."""
        F, sigma, T = 62_000.0, 0.60, 30 / 365
        approx = F * sigma * math.sqrt(T / (2 * math.pi))
        bs = bs_call_price(F, F, sigma, T)
        assert bs == pytest.approx(approx, rel=0.02)

    def test_call_price_deep_itm(self) -> None:
        """Deep ITM call ≈ F - K (intrinsic)."""
        F, K = 80_000.0, 10_000.0
        price = bs_call_price(F, K, 0.5, 30 / 365)
        assert price == pytest.approx(F - K, rel=0.01)

    def test_call_price_deep_otm_near_zero(self) -> None:
        price = bs_call_price(60_000.0, 500_000.0, 0.60, 30 / 365)
        assert price < 1.0   # near zero in USD

    def test_zero_vol_call_price_intrinsic(self) -> None:
        assert bs_call_price(62_000.0, 60_000.0, 0.0, 30 / 365) == pytest.approx(2_000.0)
        assert bs_call_price(62_000.0, 64_000.0, 0.0, 30 / 365) == pytest.approx(0.0)


class TestBSDigitalPrice:
    def test_atm_digital_below_half(self) -> None:
        """ATM digital = N(-0.5σ√T) < 0.5 for any σ, T > 0."""
        F, K, sigma, T = 62_000.0, 62_000.0, 0.65, 30 / 365
        p = bs_digital_price(F, K, sigma, T)
        assert p < 0.5
        expected = norm.cdf(-0.5 * sigma * math.sqrt(T))
        assert p == pytest.approx(expected, rel=1e-6)

    def test_deep_itm_digital_near_one(self) -> None:
        p = bs_digital_price(62_000.0, 1_000.0, 0.65, 30 / 365)
        assert p > 0.999

    def test_deep_otm_digital_near_zero(self) -> None:
        p = bs_digital_price(62_000.0, 1_000_000.0, 0.65, 30 / 365)
        assert p < 0.001

    def test_digital_at_expiry_intrinsic(self) -> None:
        assert bs_digital_price(62_000.0, 60_000.0, 0.5, 0.0) == 1.0
        assert bs_digital_price(62_000.0, 64_000.0, 0.5, 0.0) == 0.0

    def test_digital_increases_with_vol_for_otm(self) -> None:
        """OTM digital (K > F): higher vol → higher probability."""
        F, K, T = 62_000.0, 70_000.0, 30 / 365
        p_lo = bs_digital_price(F, K, 0.40, T)
        p_hi = bs_digital_price(F, K, 0.80, T)
        assert p_hi > p_lo

    def test_digital_decreases_with_vol_for_itm(self) -> None:
        """ITM digital (K < F): higher vol → lower probability."""
        F, K, T = 62_000.0, 54_000.0, 30 / 365
        p_lo = bs_digital_price(F, K, 0.40, T)
        p_hi = bs_digital_price(F, K, 0.80, T)
        assert p_hi < p_lo

    def test_result_in_unit_interval(self) -> None:
        for K in [30_000, 62_000, 100_000]:
            p = bs_digital_price(62_000.0, K, 0.65, 30 / 365)
            assert 0.0 <= p <= 1.0


# ── DigitalPricer: analytical from surface ────────────────────────────────────

class TestDigitalPricerAnalytical:
    def _surface(
        self, sigma: float = 0.65, forward: float = 62_000.0, T: float = 30 / 365
    ) -> tuple[VolSurface, datetime]:
        expiry = _expiry(30)
        params = SVIParams(a=sigma ** 2 * T * 0.9, b=0.15, rho=-0.40, m=0.0, nu=0.20)
        surface = _surface_from_svi(params, forward, expiry, T)
        return surface, expiry

    def test_price_returns_digital_price(self) -> None:
        surface, expiry = self._surface()
        pricer = DigitalPricer()
        result = pricer.price_from_surface(62_000.0, expiry, surface)
        assert result is not None
        assert isinstance(result, DigitalPrice)

    def test_bid_le_mid_le_ask(self) -> None:
        surface, expiry = self._surface()
        pricer = DigitalPricer()
        result = pricer.price_from_surface(62_000.0, expiry, surface)
        assert result is not None
        assert result.bid <= result.mid <= result.ask

    def test_probabilities_in_unit_interval(self) -> None:
        surface, expiry = self._surface()
        pricer = DigitalPricer()
        for K in [50_000, 62_000, 75_000]:
            result = pricer.price_from_surface(K, expiry, surface)
            if result is not None:
                assert 0 <= result.bid <= 1
                assert 0 <= result.mid <= 1
                assert 0 <= result.ask <= 1

    def test_itm_probability_above_half(self) -> None:
        """Deep ITM (K ≪ F) should yield probability > 0.5."""
        surface, expiry = self._surface()
        pricer = DigitalPricer()
        result = pricer.price_from_surface(40_000.0, expiry, surface)
        assert result is not None
        assert result.mid > 0.5

    def test_otm_probability_below_half(self) -> None:
        """Deep OTM (K ≫ F) should yield probability < 0.5."""
        surface, expiry = self._surface()
        pricer = DigitalPricer()
        result = pricer.price_from_surface(92_000.0, expiry, surface)
        assert result is not None
        assert result.mid < 0.5

    def test_method_label(self) -> None:
        surface, expiry = self._surface()
        pricer = DigitalPricer()
        result = pricer.price_from_surface(62_000.0, expiry, surface)
        assert result is not None
        assert result.method == "analytical"

    def test_returns_none_for_unknown_expiry(self) -> None:
        surface = VolSurface()
        pricer = DigitalPricer()
        result = pricer.price_from_surface(62_000.0, _expiry(999), surface)
        assert result is None


# ── DigitalPricer: call-spread from ticks ─────────────────────────────────────

class TestDigitalPricerCallSpread:
    def test_call_spread_close_to_analytical(self) -> None:
        """Call spread should match BS digital within a few percent."""
        F = 62_000.0
        K = 65_000.0
        sigma = 0.65
        T = 30 / 365
        delta = 1_000.0
        expiry = _expiry(30)

        # Build tick store with strikes at K-δ and K+δ
        tick_store: dict[str, OptionTick] = {}
        for strike in [K - delta, K + delta]:
            tick = _make_call_tick(strike, expiry, F, sigma, T)
            tick_store[tick.instrument_name] = tick

        pricer = DigitalPricer()
        result = pricer.price_from_ticks(K, expiry, delta, tick_store)
        assert result is not None

        analytical = bs_digital_price(F, K, sigma, T)
        assert result.mid == pytest.approx(analytical, abs=0.03)

    def test_bid_le_mid_le_ask_call_spread(self) -> None:
        F, sigma, T = 62_000.0, 0.65, 30 / 365
        K, delta = 62_000.0, 500.0
        expiry = _expiry(30)

        tick_store: dict[str, OptionTick] = {}
        for strike in [K - delta, K + delta]:
            tick = _make_call_tick(strike, expiry, F, sigma, T)
            tick_store[tick.instrument_name] = tick

        result = DigitalPricer().price_from_ticks(K, expiry, delta, tick_store)
        assert result is not None
        assert result.bid <= result.mid <= result.ask

    def test_method_label_call_spread(self) -> None:
        F, sigma, T = 62_000.0, 0.65, 30 / 365
        K, delta = 62_000.0, 500.0
        expiry = _expiry(30)

        tick_store: dict[str, OptionTick] = {}
        for strike in [K - delta, K + delta]:
            tick = _make_call_tick(strike, expiry, F, sigma, T)
            tick_store[tick.instrument_name] = tick

        result = DigitalPricer().price_from_ticks(K, expiry, delta, tick_store)
        assert result is not None
        assert result.method == "call_spread"

    def test_falls_back_to_surface_when_ticks_missing(self) -> None:
        """When tick_store is empty, synthesise from vol surface."""
        F, sigma, T = 62_000.0, 0.65, 30 / 365
        K, delta = 62_000.0, 500.0
        expiry = _expiry(30)
        params = SVIParams(a=sigma ** 2 * T * 0.9, b=0.15, rho=-0.40, m=0.0, nu=0.20)
        surface = _surface_from_svi(params, F, expiry, T)

        result = DigitalPricer().price_from_ticks(K, expiry, delta, {}, surface=surface)
        assert result is not None
        assert result.mid > 0

    def test_returns_none_when_no_ticks_and_no_surface(self) -> None:
        result = DigitalPricer().price_from_ticks(62_000.0, _expiry(), 500.0, {})
        assert result is None

    def test_call_spread_converges_as_delta_shrinks(self) -> None:
        """Smaller δ → call spread converges toward analytical digital."""
        F, sigma, T = 62_000.0, 0.65, 30 / 365
        K = 65_000.0
        expiry = _expiry(30)
        analytical = bs_digital_price(F, K, sigma, T)

        errors = []
        for delta in [2_000.0, 1_000.0, 500.0]:
            tick_store: dict[str, OptionTick] = {}
            for strike in [K - delta, K + delta]:
                t = _make_call_tick(strike, expiry, F, sigma, T)
                tick_store[t.instrument_name] = t

            result = DigitalPricer().price_from_ticks(K, expiry, delta, tick_store)
            assert result is not None
            errors.append(abs(result.mid - analytical))

        # Error should decrease (or at least not increase) as delta shrinks
        assert errors[-1] <= errors[0] + 0.005, (
            f"Call spread errors: {errors} — should converge toward analytical"
        )
