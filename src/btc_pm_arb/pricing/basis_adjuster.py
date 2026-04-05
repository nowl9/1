"""Settlement basis adjustment: P(spot > K) → P(settlement > K).

Different venues settle BTC options against different price benchmarks:

  Deribit  — 30-minute arithmetic TWAP of the Deribit BTC Index
  Kalshi   — 60-second CF Benchmarks RTI (Real-Time Index)
  Polymarket — effectively spot (resolution by human/oracle at a point in time)

Under GBM the arithmetic TWAP has *lower* variance than the terminal spot,
so P(TWAP > K) ≠ P(spot > K).  The effect is largest for near-expiry options
in high-vol regimes (e.g. vol ≥ 60 %, time-to-expiry < 1 day).

We use the Lévy (1992) lognormal approximation for the TWAP distribution:

  1. Compute E[A²]/F² exactly under GBM (r = 0, forward numeraire):
       M₂/F² = (2/(σ⁴τ²)) · [exp(σ²T) − exp(σ²t₀)·(1 + σ²τ)]
     where t₀ = T − τ is the start of the averaging window.

  2. Approximate A as lognormal with effective total variance:
       σ_L² = log(M₂/F²)

  3. Price the digital:
       P(TWAP > K) ≈ N( (log(F/K) − σ_L²/2) / σ_L )

Limiting cases:
  τ → 0  (point sample):   σ_L² → σ²T  →  recovers spot BS digital ✓
  τ → T  (full averaging): σ_L² → σ²T/3  →  effective vol ≈ σ/√3  ✓

References:
  Lévy, E. (1992).  Journal of International Money and Finance 11, 474–491.
  Turnbull & Wakeman (1991).  JFQA 26, 377–389.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from scipy.stats import norm

# ── Settlement window constants ───────────────────────────────────────────────

#: Deribit BTC options settle to a 30-minute arithmetic TWAP.
DERIBIT_TWAP_YEARS: float = 30.0 / (60.0 * 24.0 * 365.0)    # ≈ 5.71 × 10⁻⁵ yr

#: Kalshi uses the CF Benchmarks Real-Time Index averaged over 60 seconds.
KALSHI_RTI_YEARS: float = 60.0 / (60.0 * 24.0 * 365.0 * 60.0)  # ≈ 1.90 × 10⁻⁶ yr

#: Polymarket settlement is treated as a point-in-time spot read.
POLYMARKET_SPOT_YEARS: float = 0.0

SettlementType = Literal["deribit_twap", "kalshi_rti", "polymarket_spot", "unknown"]

_WINDOWS: dict[str, float] = {
    "deribit_twap":    DERIBIT_TWAP_YEARS,
    "kalshi_rti":      KALSHI_RTI_YEARS,
    "polymarket_spot": POLYMARKET_SPOT_YEARS,
    "unknown":         0.0,
}


# ── Lévy approximation core ───────────────────────────────────────────────────

def levy_twap_m2_ratio(
    sigma: float,
    time_to_expiry: float,
    window: float,
) -> float:
    """E[A²] / F² for an arithmetic TWAP over the final *window* years.

    Under risk-neutral GBM with r = 0 and forward F = E[S_T]:

        E[A²]/F² = (2 / (σ⁴τ²)) · [exp(σ²T) − exp(σ²t₀)·(1 + σ²τ)]

    where t₀ = T − τ.  Factoring out exp(σ²t₀) gives the numerically
    stable form used here:

        E[A²]/F² = (2 / (σ²τ)²) · exp(σ²t₀) · (exp(σ²τ) − 1 − σ²τ)

    The quantity (exp(x) − 1 − x) is computed via ``np.expm1`` for
    accuracy when σ²τ is tiny (e.g. τ = 60 seconds).

    Args:
        sigma:           implied vol (decimal, e.g. 0.65)
        time_to_expiry:  T in years from now to option expiry
        window:          τ in years — length of the averaging period (≤ T)

    Returns:
        M₂/F² ≥ 1.0 (Jensen's inequality).
    """
    T = time_to_expiry
    tau = min(window, T)

    if tau <= 0.0:
        # Degenerate: instantaneous sample at T → recover spot variance
        return float(np.exp(sigma ** 2 * T))

    s2 = sigma ** 2
    s2t0 = s2 * (T - tau)
    s2tau = s2 * tau

    # exp(σ²τ) − 1 − σ²τ, numerically safe for tiny σ²τ via expm1
    expm1_minus_x = float(np.expm1(s2tau)) - s2tau   # ≥ 0 always

    numerator = np.exp(s2t0) * expm1_minus_x          # = exp(σ²T) − exp(σ²t₀)·(1+σ²τ)
    denom = (s2 * tau) ** 2                            # = σ⁴τ²

    if denom < 1e-40:
        return float(np.exp(s2 * T))

    m2 = 2.0 * numerator / denom
    return float(max(m2, 1.0))   # floor at 1 for numerical safety


def levy_effective_var(
    sigma: float,
    time_to_expiry: float,
    window: float,
) -> float:
    """Effective total variance σ_L² = log(E[A²]/F²) for the Lévy approximation.

    Properties:
      τ → 0  ⟹  σ_L² → σ²T   (approaches spot total variance)
      τ = T  ⟹  σ_L² ≈ σ²T/3  (full-period average; vol reduced by 1/√3)
    """
    m2 = levy_twap_m2_ratio(sigma, time_to_expiry, window)
    return float(np.log(max(m2, 1.0 + 1e-15)))


def p_twap_above_strike(
    forward: float,
    strike: float,
    sigma: float,
    time_to_expiry: float,
    window: float,
) -> float:
    """P(TWAP > K) via the Lévy lognormal approximation.

    Under the approximation A ∼ LogNormal(μ_L, σ_L²):
        P(A > K) = N( (log(F/K) − σ_L²/2) / σ_L )

    which is identical to the BS digital formula with σ replaced by σ_L/√T.

    Args:
        forward:           F = S₀ (spot/index; r = 0 approximation)
        strike:            K in USD
        sigma:             implied vol (decimal) at (K, T) from the surface
        time_to_expiry:    T in years
        window:            τ in years — averaging window

    Returns:
        Probability in [0, 1].
    """
    if time_to_expiry <= 0:
        return 1.0 if forward > strike else 0.0
    if sigma <= 0:
        return 1.0 if forward > strike else 0.0
    if forward <= 0 or strike <= 0:
        return 0.0

    sigma_L2 = levy_effective_var(sigma, time_to_expiry, window)
    sigma_L = np.sqrt(max(sigma_L2, 1e-12))

    d2 = (np.log(forward / strike) - sigma_L2 / 2.0) / sigma_L
    return float(norm.cdf(d2))


# ── BasisAdjuster class ───────────────────────────────────────────────────────

class BasisAdjuster:
    """Adjust risk-neutral P(spot > K) → P(settlement > K) for a given venue.

    Usage::

        adj = BasisAdjuster()
        p = adj.adjust(
            prob_spot=0.55,
            forward=62_000,
            strike=63_000,
            sigma=0.65,
            time_to_expiry=30 / 365,
            settlement_type="deribit_twap",
        )
    """

    def adjust(
        self,
        prob_spot: float,
        forward: float,
        strike: float,
        sigma: float,
        time_to_expiry: float,
        settlement_type: SettlementType = "deribit_twap",
    ) -> float:
        """Return P(settlement > K) for the specified settlement mechanism.

        For "polymarket_spot" and "unknown" the input probability is returned
        unchanged.  For Deribit and Kalshi, the Lévy TWAP approximation is
        applied — the adjusted probability is recomputed from scratch rather
        than adding a delta to prob_spot, which is more accurate.
        """
        window = _WINDOWS.get(settlement_type, 0.0)
        if window <= 0.0:
            return float(prob_spot)

        return p_twap_above_strike(forward, strike, sigma, time_to_expiry, window)

    def basis(
        self,
        forward: float,
        strike: float,
        sigma: float,
        time_to_expiry: float,
        from_type: SettlementType,
        to_type: SettlementType,
    ) -> float:
        """Return P(to_settlement > K) − P(from_settlement > K).

        Useful for computing the raw basis between two venues, e.g.
        Deribit TWAP vs Kalshi RTI.
        """
        from btc_pm_arb.pricing.digital_pricer import bs_digital_price

        def _p(stype: SettlementType) -> float:
            window = _WINDOWS.get(stype, 0.0)
            if window <= 0.0:
                return bs_digital_price(forward, strike, sigma, time_to_expiry)
            return p_twap_above_strike(forward, strike, sigma, time_to_expiry, window)

        return _p(to_type) - _p(from_type)
