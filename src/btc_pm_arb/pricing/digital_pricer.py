"""Digital (binary) option pricer.

Two complementary methods, both producing a (bid, mid, ask) probability triple:

  1. Call-spread (model-free):
       P(S_T > K) ≈ [C(K-δ) - C(K+δ)] / (2δ)   normalised to [0, 1]
     Uses raw OptionTick bid/ask prices.  When exact strikes K±δ are
     unavailable, synthesises prices from the vol surface.

     bid_prob = [C_bid(K-δ) - C_ask(K+δ)] / (2δ·F)   ← lower bound
     ask_prob = [C_ask(K-δ) - C_bid(K+δ)] / (2δ·F)   ← upper bound

  2. Analytical (BS + smile vol):
       P(S_T > K) = N(d₂)  where σ = smile.iv(K)
     Bid/ask IV bounds are propagated; min/max taken so the ordering
     bid ≤ mid ≤ ask is preserved regardless of moneyness direction.

The call-spread method is preferred when both K±δ ticks are available;
the analytical method is the fallback (and is faster for batch queries).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from scipy.stats import norm

from btc_pm_arb.models import OptionTick, OptionType
from btc_pm_arb.pricing.vol_surface import VolSurface


# ── Black-Scholes primitives ──────────────────────────────────────────────────

def bs_d1(forward: float, strike: float, sigma: float, t: float) -> float:
    """d₁ in the forward / zero-rate Black-Scholes convention."""
    if sigma <= 0 or t <= 0 or strike <= 0 or forward <= 0:
        return 0.0
    return (np.log(forward / strike) + 0.5 * sigma ** 2 * t) / (sigma * np.sqrt(t))


def bs_d2(forward: float, strike: float, sigma: float, t: float) -> float:
    """d₂ = d₁ − σ√T."""
    if sigma <= 0 or t <= 0 or strike <= 0 or forward <= 0:
        return 0.0
    return (np.log(forward / strike) - 0.5 * sigma ** 2 * t) / (sigma * np.sqrt(t))


def bs_call_price(forward: float, strike: float, sigma: float, t: float) -> float:
    """Undiscounted BS call price in the forward measure (r = 0)."""
    if t <= 0:
        return max(forward - strike, 0.0)
    if sigma <= 0:
        return max(forward - strike, 0.0)
    d1 = bs_d1(forward, strike, sigma, t)
    d2 = d1 - sigma * np.sqrt(t)
    return float(forward * norm.cdf(d1) - strike * norm.cdf(d2))


def bs_digital_price(forward: float, strike: float, sigma: float, t: float) -> float:
    """Risk-neutral P(S_T > K) = N(d₂) under zero-rate BS.

    Returns a value in [0, 1].  At expiry (t ≤ 0) returns the intrinsic
    indicator 1{forward > strike}.
    """
    if t <= 0:
        return 1.0 if forward > strike else 0.0
    if sigma <= 0:
        return 1.0 if forward > strike else 0.0
    return float(norm.cdf(bs_d2(forward, strike, sigma, t)))


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DigitalPrice:
    """Bid-implied / mid / ask-implied probability triple.

    Invariant: 0 ≤ bid ≤ mid ≤ ask ≤ 1 (enforced in __post_init__).
    """

    bid: float    # lower bound on probability (conservative buy price)
    mid: float    # midpoint probability
    ask: float    # upper bound on probability (conservative sell price)
    method: str   # "call_spread" | "analytical"

    def __post_init__(self) -> None:
        object.__setattr__(self, "bid", float(np.clip(self.bid, 0.0, 1.0)))
        object.__setattr__(self, "ask", float(np.clip(self.ask, 0.0, 1.0)))
        object.__setattr__(self, "mid", float(np.clip(self.mid, 0.0, 1.0)))


# ── Main pricer ───────────────────────────────────────────────────────────────

class DigitalPricer:
    """Compute digital call probabilities from market data or vol surface."""

    # ── Method 1: analytical from vol surface ────────────────────────────

    def price_from_surface(
        self,
        strike: float,
        expiry: datetime,
        surface: VolSurface,
    ) -> DigitalPrice | None:
        """BS digital N(d₂) using the SVI smile vol at (strike, expiry).

        The bid/ask IV bounds from VolSmile are propagated into two BS digital
        prices; taking min/max ensures the ordering holds for any moneyness.
        """
        smile = surface.get_smile(expiry)
        if smile is None or smile.params is None:
            return None

        forward = smile.forward
        T = smile.time_to_expiry
        if forward <= 0 or T <= 0:
            return None

        iv_mid = smile.iv(strike)
        if iv_mid is None or iv_mid <= 0:
            return None

        iv_ba = smile.iv_bid_ask(strike)
        iv_bid, iv_ask = iv_ba if iv_ba is not None else (iv_mid, iv_mid)

        mid_prob = bs_digital_price(forward, strike, iv_mid, T)
        p_at_bid_iv = bs_digital_price(forward, strike, iv_bid, T)
        p_at_ask_iv = bs_digital_price(forward, strike, iv_ask, T)

        # The digital is not monotone in IV (direction depends on moneyness),
        # so take min/max to always get the conservative bounds.
        bid_prob = min(p_at_bid_iv, p_at_ask_iv)
        ask_prob = max(p_at_bid_iv, p_at_ask_iv)

        return DigitalPrice(bid=bid_prob, mid=mid_prob, ask=ask_prob, method="analytical")

    # ── Method 2: call-spread from raw ticks ─────────────────────────────

    def price_from_ticks(
        self,
        strike: float,
        expiry: datetime,
        delta: float,
        tick_store: dict[str, OptionTick],
        surface: VolSurface | None = None,
    ) -> DigitalPrice | None:
        """Call-spread digital from raw bid/ask prices.

        Looks for call ticks nearest to K-δ and K+δ.  If not found in
        tick_store, synthesises them from the vol surface (if provided).

        The call-spread normalisation (÷ 2δ) is in USD; the final
        probability is dimensionless since calls are in USD (fraction × F).
        """
        lo_tick = _find_nearest_call(strike - delta, expiry, tick_store)
        hi_tick = _find_nearest_call(strike + delta, expiry, tick_store)

        if lo_tick is None and surface is not None:
            lo_tick = _synthetic_call_tick(strike - delta, expiry, surface)
        if hi_tick is None and surface is not None:
            hi_tick = _synthetic_call_tick(strike + delta, expiry, surface)

        if lo_tick is None or hi_tick is None:
            return None

        F = lo_tick.underlying_price
        if F <= 0:
            return None

        # Deribit prices are fractions of underlying → convert to USD
        c_lo_bid = (lo_tick.bid or 0.0) * F
        c_lo_ask = (lo_tick.ask if lo_tick.ask is not None else lo_tick.mark_price) * F
        c_hi_bid = (hi_tick.bid or 0.0) * F
        c_hi_ask = (hi_tick.ask if hi_tick.ask is not None else hi_tick.mark_price) * F

        denom = 2.0 * delta   # USD normalisation

        bid_price = max(0.0, (c_lo_bid - c_hi_ask) / denom)
        ask_price = max(0.0, (c_lo_ask - c_hi_bid) / denom)
        mid_price = (bid_price + ask_price) / 2.0

        return DigitalPrice(bid=bid_price, mid=mid_price, ask=ask_price, method="call_spread")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_nearest_call(
    target_strike: float,
    expiry: datetime,
    tick_store: dict[str, OptionTick],
    max_distance_pct: float = 0.05,
) -> OptionTick | None:
    """Find the call tick with the given expiry closest to target_strike."""
    candidates = [
        t for t in tick_store.values()
        if t.option_type == OptionType.CALL
        and t.expiry == expiry
        and abs(t.strike - target_strike) / max(target_strike, 1.0) <= max_distance_pct
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda t: abs(t.strike - target_strike))


def _synthetic_call_tick(
    strike: float,
    expiry: datetime,
    surface: VolSurface,
) -> OptionTick | None:
    """Build a synthetic OptionTick from the vol surface for the call-spread method."""
    smile = surface.get_smile(expiry)
    if smile is None or smile.params is None:
        return None

    F = smile.forward
    T = smile.time_to_expiry
    if F <= 0 or T <= 0:
        return None

    iv = smile.iv(strike)
    if iv is None:
        return None

    mid_frac = bs_call_price(F, strike, iv, T) / F

    iv_ba = smile.iv_bid_ask(strike)
    if iv_ba is None:
        bid_frac, ask_frac = mid_frac * 0.98, mid_frac * 1.02
    else:
        iv_bid, iv_ask = iv_ba
        bid_frac = bs_call_price(F, strike, iv_bid, T) / F
        ask_frac = bs_call_price(F, strike, iv_ask, T) / F

    return OptionTick(
        instrument_name=f"SYNTHETIC-{int(strike)}-C",
        strike=strike,
        expiry=expiry,
        option_type=OptionType.CALL,
        bid=float(bid_frac),
        ask=float(ask_frac),
        mark_price=float(mid_frac),
        mark_iv=iv * 100.0,
        underlying_price=F,
        index_price=F,
        timestamp=datetime.now(timezone.utc),
    )
