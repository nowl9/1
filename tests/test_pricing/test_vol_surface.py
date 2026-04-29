"""Tests for vol_surface.py: SVI parameterization, fitting, and VolSurface."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from btc_pm_arb.feeds.deribit import parse_instrument
from btc_pm_arb.models import OptionTick, OptionType
from btc_pm_arb.pricing.vol_surface import SVIParams, VolSmile, VolSurface, _fit_svi


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expiry(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def _make_tick(
    strike: float,
    expiry: datetime,
    mark_iv: float,
    forward: float = 62_000.0,
    bid_iv: float | None = None,
    ask_iv: float | None = None,
) -> OptionTick:
    return OptionTick(
        instrument_name=f"BTC-TEST-{int(strike)}-C",
        strike=strike,
        expiry=expiry,
        option_type=OptionType.CALL,
        mark_price=0.02,
        mark_iv=mark_iv,
        bid_iv=bid_iv,
        ask_iv=ask_iv,
        underlying_price=forward,
        index_price=forward,
        timestamp=datetime.now(timezone.utc),
    )


def _svi_smile_ticks(
    params: SVIParams,
    forward: float,
    T: float,
    expiry: datetime,
    strikes: list[float],
) -> list[OptionTick]:
    """Generate synthetic ticks whose mark_iv comes from known SVI params."""
    ticks = []
    for K in strikes:
        k = np.log(K / forward)
        w = float(params.total_var(np.array([k]))[0])
        iv_pct = math.sqrt(max(w / T, 0.0)) * 100.0
        ticks.append(_make_tick(K, expiry, mark_iv=iv_pct, forward=forward))
    return ticks


# ── SVIParams unit tests ──────────────────────────────────────────────────────

class TestSVIParams:
    """Test the SVIParams dataclass formulas directly."""

    def _typical_btc_params(self) -> SVIParams:
        # Typical BTC smile: moderate width, strong put skew
        return SVIParams(a=0.04, b=0.25, rho=-0.45, m=0.05, nu=0.15)

    def test_total_var_is_non_negative(self) -> None:
        params = self._typical_btc_params()
        k = np.linspace(-3.0, 3.0, 500)
        w = params.total_var(k)
        assert np.all(w >= 0), "Total variance must be non-negative"

    def test_total_var_shape(self) -> None:
        params = self._typical_btc_params()
        k = np.array([-1.0, 0.0, 1.0])
        assert params.total_var(k).shape == (3,)

    def test_iv_positive(self) -> None:
        params = self._typical_btc_params()
        k = np.linspace(-1.5, 1.5, 100)
        T = 30 / 365
        iv = params.iv(k, T)
        assert np.all(iv > 0)

    def test_iv_at_atm(self) -> None:
        """ATM IV should equal sqrt(a + b*nu) / sqrt(T) approximately."""
        params = SVIParams(a=0.04, b=0.20, rho=0.0, m=0.0, nu=0.10)
        T = 30 / 365
        w_atm = float(params.total_var(np.array([0.0]))[0])
        expected_iv = math.sqrt(w_atm / T)
        assert params.iv(np.array([0.0]), T)[0] == pytest.approx(expected_iv, rel=1e-6)

    def test_put_skew_makes_otm_puts_expensive(self) -> None:
        """With rho < 0, OTM put (k < 0) IV should exceed OTM call (k > 0) IV."""
        params = SVIParams(a=0.04, b=0.25, rho=-0.5, m=0.0, nu=0.15)
        T = 30 / 365
        iv_otm_put = float(params.iv(np.array([-0.5]), T)[0])
        iv_otm_call = float(params.iv(np.array([+0.5]), T)[0])
        assert iv_otm_put > iv_otm_call, "Put skew (rho<0) should make OTM puts pricier"

    def test_durrleman_g_is_non_negative_for_valid_params(self) -> None:
        """Well-constrained SVI should satisfy the Durrleman condition."""
        params = SVIParams(a=0.04, b=0.15, rho=-0.40, m=0.0, nu=0.20)
        assert not params.has_butterfly_arbitrage(k_lo=-2.5, k_hi=2.5)

    def test_min_var_constraint(self) -> None:
        params = self._typical_btc_params()
        assert params.min_var_ok()

    def test_lee_constraint(self) -> None:
        params = self._typical_btc_params()
        assert params.lee_ok()

    def test_derivatives_consistency(self) -> None:
        """Numerical derivative should match analytical dw/dk."""
        params = self._typical_btc_params()
        k0 = np.array([0.3])
        eps = 1e-6
        numerical = (
            params.total_var(k0 + eps) - params.total_var(k0 - eps)
        ) / (2 * eps)
        analytical = params.dtotal_var_dk(k0)
        np.testing.assert_allclose(analytical, numerical, rtol=1e-4)

    def test_second_derivative_positive(self) -> None:
        """SVI second derivative d²w/dk² is always positive (convex smile)."""
        params = self._typical_btc_params()
        k = np.linspace(-3.0, 3.0, 300)
        d2 = params.d2total_var_dk2(k)
        assert np.all(d2 > 0)


# ── _fit_svi tests ────────────────────────────────────────────────────────────

class TestFitSVI:
    """Test the SVI fitting function."""

    def _synthetic_data(
        self, params: SVIParams, n: int = 15
    ) -> tuple[np.ndarray, np.ndarray]:
        k = np.linspace(-1.0, 1.0, n)
        w = params.total_var(k)
        return k, w

    def test_fit_recovers_surface(self) -> None:
        """Fitted SVI should closely reproduce the true total-variance curve."""
        true_params = SVIParams(a=0.04, b=0.20, rho=-0.40, m=0.05, nu=0.15)
        k, w_true = self._synthetic_data(true_params)

        fitted, rmse = _fit_svi(k, w_true)

        assert fitted is not None, "Fit should converge on clean data"
        assert rmse < 1e-5, f"RMSE too high: {rmse}"

        # Verify the fitted *surface* (not just params) matches
        k_test = np.linspace(-2.0, 2.0, 200)
        np.testing.assert_allclose(
            fitted.total_var(k_test),
            true_params.total_var(k_test),
            rtol=1e-3,
        )

    def test_fit_satisfies_no_arb_constraints(self) -> None:
        """Fit must produce params with min-var and Lee constraints satisfied."""
        params = SVIParams(a=0.05, b=0.18, rho=-0.35, m=0.0, nu=0.20)
        k, w = self._synthetic_data(params)
        fitted, _ = _fit_svi(k, w)

        assert fitted is not None
        assert fitted.min_var_ok(), "Minimum variance constraint violated"
        assert fitted.lee_ok(), "Lee moment formula violated"

    def test_fitted_surface_has_no_butterfly_arb(self) -> None:
        """Durrleman condition g(k) ≥ 0 must hold on the fitted surface."""
        params = SVIParams(a=0.04, b=0.15, rho=-0.45, m=0.0, nu=0.18)
        k, w = self._synthetic_data(params, n=20)
        fitted, _ = _fit_svi(k, w)

        assert fitted is not None
        assert not fitted.has_butterfly_arbitrage(
            k_lo=-2.0, k_hi=2.0
        ), "Butterfly arbitrage detected in fitted surface"

    def test_fit_with_skewed_data(self) -> None:
        """Fit to heavy put-skew data (typical BTC) should give rho < 0."""
        params = SVIParams(a=0.04, b=0.30, rho=-0.65, m=-0.10, nu=0.10)
        k, w = self._synthetic_data(params, n=18)
        fitted, rmse = _fit_svi(k, w)

        assert fitted is not None
        assert rmse < 1e-4
        assert fitted.rho < 0, "Should recover negative rho for put-skewed input"

    def test_returns_none_for_insufficient_data(self) -> None:
        k = np.array([0.0, 0.1])
        w = np.array([0.04, 0.05])
        result, rmse = _fit_svi(k, w)
        assert result is None
        assert rmse == float("inf")


# ── VolSmile tests ────────────────────────────────────────────────────────────

class TestVolSmile:
    def test_smile_update_fits_params(self) -> None:
        T = 30 / 365
        expiry = _expiry(30)
        forward = 62_000.0
        true_params = SVIParams(a=0.04, b=0.20, rho=-0.40, m=0.0, nu=0.15)

        strikes = [50_000, 55_000, 58_000, 60_000, 62_000, 64_000, 66_000,
                   68_000, 72_000, 76_000, 80_000]
        ticks = _svi_smile_ticks(true_params, forward, T, expiry, strikes)

        smile = VolSmile(expiry=expiry, forward=forward, time_to_expiry=T)
        smile.update(ticks)

        assert smile.params is not None
        assert smile.fit_rmse < 0.01   # reasonable fit
        assert smile.n_options == len(ticks)

    def test_smile_iv_at_atm(self) -> None:
        T = 30 / 365
        expiry = _expiry(30)
        forward = 62_000.0
        true_params = SVIParams(a=0.04, b=0.20, rho=0.0, m=0.0, nu=0.15)

        strikes = [50_000, 55_000, 58_000, 60_000, 62_000, 64_000, 68_000,
                   72_000, 76_000, 80_000, 85_000]
        ticks = _svi_smile_ticks(true_params, forward, T, expiry, strikes)

        smile = VolSmile(expiry=expiry, forward=forward, time_to_expiry=T)
        smile.update(ticks)

        expected_w_atm = float(true_params.total_var(np.array([0.0]))[0])
        expected_iv = math.sqrt(expected_w_atm / T)
        fitted_iv = smile.iv(forward)   # strike = forward → k = 0

        assert fitted_iv is not None
        assert fitted_iv == pytest.approx(expected_iv, rel=0.01)

    def test_smile_returns_none_with_no_params(self) -> None:
        smile = VolSmile(expiry=_expiry(), forward=62_000.0, time_to_expiry=30 / 365)
        assert smile.iv(60_000) is None

    def test_smile_fallback_flat_vol_when_few_ticks(self) -> None:
        """With < 5 ticks, should fall back to flat smile at median IV."""
        expiry = _expiry(30)
        ticks = [_make_tick(62_000, expiry, mark_iv=65.0)]
        smile = VolSmile(expiry=expiry, forward=62_000.0, time_to_expiry=30 / 365)
        smile.update(ticks)

        assert smile.params is not None
        iv = smile.iv(62_000)
        assert iv is not None
        assert iv == pytest.approx(0.65, rel=0.05)

    def test_smile_iv_bid_ask_spread(self) -> None:
        expiry = _expiry(30)
        ticks = [
            _make_tick(K, expiry, mark_iv=65.0, bid_iv=62.0, ask_iv=68.0)
            for K in [50_000, 55_000, 58_000, 60_000, 62_000, 64_000,
                      66_000, 70_000, 74_000, 78_000, 82_000]
        ]
        smile = VolSmile(expiry=expiry, forward=62_000.0, time_to_expiry=30 / 365)
        smile.update(ticks)

        result = smile.iv_bid_ask(62_000)
        assert result is not None
        bid_iv, ask_iv = result
        assert bid_iv < ask_iv, "Bid IV must be less than ask IV"
        assert bid_iv > 0


# ── VolSurface tests ──────────────────────────────────────────────────────────

class TestVolSurface:
    def _make_surface_ticks(
        self, params: SVIParams, forward: float, expiry: datetime, T: float
    ) -> list[OptionTick]:
        strikes = [50_000, 55_000, 58_000, 60_000, 62_000, 64_000, 66_000,
                   70_000, 74_000, 78_000, 84_000]
        return _svi_smile_ticks(params, forward, T, expiry, strikes)

    def test_update_returns_dirty_expiries(self) -> None:
        surface = VolSurface()
        expiry = _expiry(30)
        params = SVIParams(a=0.04, b=0.20, rho=-0.40, m=0.0, nu=0.15)
        ticks = self._make_surface_ticks(params, 62_000.0, expiry, 30 / 365)

        dirty = surface.update(ticks)
        assert expiry in dirty

    def test_second_identical_update_is_no_op(self) -> None:
        surface = VolSurface()
        expiry = _expiry(30)
        params = SVIParams(a=0.04, b=0.20, rho=-0.40, m=0.0, nu=0.15)
        ticks = self._make_surface_ticks(params, 62_000.0, expiry, 30 / 365)

        surface.update(ticks)
        dirty2 = surface.update(ticks)   # same mark_ivs → nothing changed
        assert len(dirty2) == 0

    def test_iv_query_after_update(self) -> None:
        surface = VolSurface()
        expiry = _expiry(30)
        T = 30 / 365
        forward = 62_000.0
        params = SVIParams(a=0.04, b=0.20, rho=-0.40, m=0.0, nu=0.15)
        ticks = self._make_surface_ticks(params, forward, expiry, T)

        surface.update(ticks)
        iv = surface.iv(strike=62_000, expiry=expiry)

        assert iv is not None
        assert 0.01 < iv < 5.0   # sanity: IV in (1%, 500%)

    def test_term_structure_interpolation(self) -> None:
        """Query between two fitted expiries should interpolate correctly."""
        surface = VolSurface()
        params = SVIParams(a=0.04, b=0.18, rho=-0.40, m=0.0, nu=0.15)
        forward = 62_000.0

        exp30 = _expiry(30)
        exp90 = _expiry(90)
        mid_exp = _expiry(60)

        ticks30 = self._make_surface_ticks(params, forward, exp30, 30 / 365)
        ticks90 = self._make_surface_ticks(params, forward, exp90, 90 / 365)

        surface.update(ticks30 + ticks90)

        iv_30 = surface.iv(62_000, exp30)
        iv_90 = surface.iv(62_000, exp90)
        iv_mid = surface.iv(62_000, mid_exp)

        assert iv_30 is not None and iv_90 is not None and iv_mid is not None
        # Interpolated IV should be between the two endpoints (total-var is linear)
        lo, hi = sorted([iv_30, iv_90])
        assert lo * 0.95 <= iv_mid <= hi * 1.05, (
            f"Interpolated IV {iv_mid:.4f} outside [{lo:.4f}, {hi:.4f}]"
        )

    def test_all_expiries(self) -> None:
        surface = VolSurface()
        params = SVIParams(a=0.04, b=0.18, rho=-0.40, m=0.0, nu=0.15)
        forward = 62_000.0

        for days in [30, 60, 90]:
            exp = _expiry(days)
            ticks = self._make_surface_ticks(params, forward, exp, days / 365)
            surface.update(ticks)

        expiries = surface.all_expiries()
        assert len(expiries) == 3
        assert expiries == sorted(expiries)
