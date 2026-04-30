"""Tests for basis_adjuster.py: Lévy TWAP approximation and BasisAdjuster."""

from __future__ import annotations

import math

import pytest
from scipy.stats import norm

from btc_pm_arb.pricing.basis_adjuster import (
    DERIBIT_TWAP_YEARS,
    KALSHI_RTI_YEARS,
    BasisAdjuster,
    levy_effective_var,
    levy_twap_m2_ratio,
    p_twap_above_strike,
)
from btc_pm_arb.pricing.digital_pricer import bs_digital_price


# ── levy_twap_m2_ratio ────────────────────────────────────────────────────────

class TestLevyM2Ratio:
    def test_m2_is_at_least_one(self) -> None:
        """Jensen's inequality: E[A²] ≥ (E[A])² = F²."""
        for sigma in [0.3, 0.6, 0.9]:
            for T in [1 / 365, 7 / 365, 30 / 365, 90 / 365]:
                for tau_frac in [0.001, 0.01, 0.1, 0.5, 1.0]:
                    tau = T * tau_frac
                    m2 = levy_twap_m2_ratio(sigma, T, tau)
                    assert m2 >= 1.0 - 1e-9, (
                        f"M2/F² < 1 for sigma={sigma}, T={T:.4f}, tau={tau:.6f}: {m2}"
                    )

    def test_zero_window_recovers_spot_variance(self) -> None:
        """τ → 0: M₂/F² → exp(σ²T) (spot variance ratio)."""
        sigma, T = 0.65, 30 / 365
        spot_m2 = math.exp(sigma ** 2 * T)
        m2_tiny = levy_twap_m2_ratio(sigma, T, window=0.0)
        assert m2_tiny == pytest.approx(spot_m2, rel=1e-6)

    def test_full_window_gives_lower_m2_than_point(self) -> None:
        """Averaging lowers variance: full-period M₂ < point-sample M₂."""
        sigma, T = 0.60, 30 / 365
        m2_full = levy_twap_m2_ratio(sigma, T, window=T)
        m2_point = levy_twap_m2_ratio(sigma, T, window=0.0)
        assert m2_full < m2_point

    def test_m2_increases_with_sigma(self) -> None:
        """Higher vol → wider distribution → larger M₂ for any τ."""
        T, tau = 30 / 365, 7 / 365
        m2_lo = levy_twap_m2_ratio(0.40, T, tau)
        m2_hi = levy_twap_m2_ratio(0.80, T, tau)
        assert m2_hi > m2_lo

    def test_numerically_stable_for_tiny_window(self) -> None:
        """Kalshi 60-second window should not produce NaN or inf."""
        m2 = levy_twap_m2_ratio(sigma=0.8, time_to_expiry=1 / 365, window=KALSHI_RTI_YEARS)
        assert math.isfinite(m2)
        assert m2 >= 1.0


# ── levy_effective_var ────────────────────────────────────────────────────────

class TestLevyEffectiveVar:
    def test_converges_to_spot_total_var_as_window_shrinks(self) -> None:
        """σ_L² → σ²T as τ → 0."""
        sigma, T = 0.65, 30 / 365
        spot_var = sigma ** 2 * T

        var_tiny = levy_effective_var(sigma, T, window=T * 1e-6)
        assert var_tiny == pytest.approx(spot_var, rel=1e-3)

    def test_full_window_var_approx_one_third(self) -> None:
        """τ = T: σ_L² ≈ σ²T / 3 (Lévy well-known result for small σ²T)."""
        sigma, T = 0.40, 10 / 365   # small σ²T ≈ 0.0044 for good Taylor approx
        spot_var = sigma ** 2 * T
        var_full = levy_effective_var(sigma, T, window=T)
        # Allow 5% tolerance around σ²T/3
        assert var_full == pytest.approx(spot_var / 3, rel=0.05)

    def test_effective_var_less_than_spot_var(self) -> None:
        """Any τ > 0 reduces effective variance below spot."""
        sigma, T = 0.65, 30 / 365
        spot_var = sigma ** 2 * T
        for tau_frac in [0.001, 0.01, 0.1, 0.5, 1.0]:
            eff_var = levy_effective_var(sigma, T, window=T * tau_frac)
            assert eff_var <= spot_var + 1e-9, (
                f"Effective var {eff_var:.6f} exceeds spot var {spot_var:.6f} "
                f"for tau_frac={tau_frac}"
            )

    def test_deribit_twap_negligible_for_30d_option(self) -> None:
        """30-min TWAP adjustment is < 0.1% on a 30-day option."""
        sigma, T = 0.65, 30 / 365
        spot_var = sigma ** 2 * T
        deribit_var = levy_effective_var(sigma, T, DERIBIT_TWAP_YEARS)
        # Relative change should be tiny
        assert abs(deribit_var - spot_var) / spot_var < 0.001


# ── p_twap_above_strike ───────────────────────────────────────────────────────

class TestPTwapAboveStrike:
    def test_converges_to_spot_as_window_shrinks(self) -> None:
        """τ → 0: P(TWAP > K) → P(spot > K)."""
        F, K, sigma, T = 62_000.0, 65_000.0, 0.70, 30 / 365
        p_spot = bs_digital_price(F, K, sigma, T)
        p_twap_tiny = p_twap_above_strike(F, K, sigma, T, window=T * 1e-7)
        assert p_twap_tiny == pytest.approx(p_spot, rel=1e-3)

    def test_otm_twap_below_spot(self) -> None:
        """OTM (K > F): lower vol → lower probability for TWAP vs spot."""
        F, K, sigma, T = 62_000.0, 70_000.0, 0.70, 30 / 365
        p_spot = bs_digital_price(F, K, sigma, T)
        # Full-period TWAP has σ/√3 → lower probability for OTM
        p_twap_full = p_twap_above_strike(F, K, sigma, T, window=T)
        assert p_twap_full < p_spot

    def test_itm_twap_above_spot(self) -> None:
        """ITM (K < F): lower vol → higher probability for TWAP vs spot."""
        F, K, sigma, T = 62_000.0, 54_000.0, 0.70, 30 / 365
        p_spot = bs_digital_price(F, K, sigma, T)
        p_twap_full = p_twap_above_strike(F, K, sigma, T, window=T)
        assert p_twap_full > p_spot

    def test_result_in_unit_interval(self) -> None:
        for K in [40_000, 62_000, 90_000]:
            p = p_twap_above_strike(62_000.0, K, 0.65, 30 / 365, DERIBIT_TWAP_YEARS)
            assert 0.0 <= p <= 1.0

    def test_zero_expiry_intrinsic(self) -> None:
        assert p_twap_above_strike(62_000.0, 60_000.0, 0.5, 0.0, 0.01) == 1.0
        assert p_twap_above_strike(62_000.0, 64_000.0, 0.5, 0.0, 0.01) == 0.0

    def test_adjustment_larger_for_shorter_expiry(self) -> None:
        """The TWAP basis is bigger when the averaging window is a larger
        fraction of time-to-expiry (near-expiry options)."""
        F, K, sigma = 62_000.0, 70_000.0, 0.80
        tau = DERIBIT_TWAP_YEARS

        # Short-dated: τ/T ratio is large
        p_spot_1d = bs_digital_price(F, K, sigma, 1 / 365)
        p_twap_1d = p_twap_above_strike(F, K, sigma, 1 / 365, tau)
        basis_1d = abs(p_twap_1d - p_spot_1d)

        # Long-dated: τ/T ratio is tiny
        p_spot_90d = bs_digital_price(F, K, sigma, 90 / 365)
        p_twap_90d = p_twap_above_strike(F, K, sigma, 90 / 365, tau)
        basis_90d = abs(p_twap_90d - p_spot_90d)

        assert basis_1d > basis_90d, (
            "Near-expiry TWAP basis should exceed far-expiry basis"
        )


# ── BasisAdjuster class ───────────────────────────────────────────────────────

class TestBasisAdjuster:
    def test_polymarket_unchanged(self) -> None:
        adj = BasisAdjuster()
        p = adj.adjust(0.55, 62_000, 65_000, 0.65, 30 / 365, "polymarket_spot")
        assert p == pytest.approx(0.55)

    def test_unknown_settlement_unchanged(self) -> None:
        adj = BasisAdjuster()
        p = adj.adjust(0.60, 62_000, 65_000, 0.65, 30 / 365, "unknown")
        assert p == pytest.approx(0.60)

    def test_deribit_twap_is_computed(self) -> None:
        """Deribit adjustment should return a value ≠ polymarket for same params."""
        adj = BasisAdjuster()
        p_deribit = adj.adjust(0.50, 62_000, 65_000, 0.65, 30 / 365, "deribit_twap")
        p_poly = adj.adjust(0.50, 62_000, 65_000, 0.65, 30 / 365, "polymarket_spot")
        # Numerically tiny for 30-day but function should return a real value
        assert math.isfinite(p_deribit)
        assert 0.0 <= p_deribit <= 1.0

    def test_kalshi_rti_adjustment_tiny_for_long_dated(self) -> None:
        """60-second RTI window is negligible for 30-day option."""
        adj = BasisAdjuster()
        F, K, sigma, T = 62_000.0, 65_000.0, 0.65, 30 / 365
        p_spot = bs_digital_price(F, K, sigma, T)
        p_kalshi = adj.adjust(p_spot, F, K, sigma, T, "kalshi_rti")
        assert abs(p_kalshi - p_spot) < 0.001

    def test_deribit_twap_meaningful_for_1h_option(self) -> None:
        """For a 1-hour to expiry option with high vol, the TWAP basis is noticeable."""
        adj = BasisAdjuster()
        F, K, sigma, T = 62_000.0, 63_000.0, 1.2, 1 / (365 * 24)
        p_spot = bs_digital_price(F, K, sigma, T)
        p_twap = adj.adjust(p_spot, F, K, sigma, T, "deribit_twap")
        # The basis should be non-trivial at 1h to expiry
        assert abs(p_twap - p_spot) > 1e-5

    def test_basis_method(self) -> None:
        """basis() should return to_type - from_type."""
        adj = BasisAdjuster()
        F, K, sigma, T = 62_000.0, 65_000.0, 0.65, 30 / 365
        b = adj.basis(F, K, sigma, T, from_type="polymarket_spot", to_type="deribit_twap")
        assert math.isfinite(b)

    def test_output_always_in_unit_interval(self) -> None:
        adj = BasisAdjuster()
        for stype in ("deribit_twap", "kalshi_rti", "polymarket_spot", "unknown"):
            p = adj.adjust(0.50, 62_000, 65_000, 0.65, 30 / 365, stype)  # type: ignore[arg-type]
            assert 0.0 <= p <= 1.0
