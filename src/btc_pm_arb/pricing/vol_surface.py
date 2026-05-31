"""Implied volatility surface from Deribit OptionTick stream.

Uses Gatheral's raw SVI parameterization per expiry slice:

    w(k) = a + b * (ρ*(k-m) + sqrt((k-m)² + ν²))

where
    k = log(K/F)   log-moneyness
    w = σ²·T       total implied variance
    ν              curvature at the vertex (NOT implied vol — named `nu` in code)

Interpolates total implied variance linearly across the term structure for
(strike, expiry) pairs that lie between fitted slices.

BTC characteristics:
  - Pronounced put skew → ρ typically −0.3 to −0.7
  - Wide smile → b can be 0.1–0.4

References:
  Gatheral, J. (2004). "A parsimonious arbitrage-free implied volatility
  parameterization with application to the valuation of volatility derivatives."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import structlog
from scipy.optimize import minimize

from btc_pm_arb.models import OptionTick

logger: structlog.BoundLogger = structlog.get_logger(__name__)

_MIN_FIT_POINTS = 5
_SECONDS_PER_YEAR = 365.25 * 86400.0


# ── SVI parameterization ──────────────────────────────────────────────────────

@dataclass
class SVIParams:
    """Gatheral raw-SVI parameters for one expiry slice.

    The total implied variance surface is:
        w(k) = a + b * (ρ*(k-m) + sqrt((k-m)² + ν²))

    No-arbitrage requirements (checked separately):
        (1) a + b·ν·sqrt(1-ρ²) ≥ 0   (non-negative variance everywhere)
        (2) b·(1+|ρ|) ≤ 2              (Lee's moment formula)
        (3) Durrleman's g(k) ≥ 0       (no butterfly arbitrage)
    """

    a: float    # overall variance level
    b: float    # smile width / angle, b > 0
    rho: float  # skew, |rho| < 1; negative = put skew
    m: float    # ATM shift in log-moneyness
    nu: float   # vertex smoothness, nu > 0

    # ── core formula ─────────────────────────────────────────────────────

    def total_var(self, k: np.ndarray) -> np.ndarray:
        """w(k): total implied variance for array of log-moneynesses."""
        km = k - self.m
        return self.a + self.b * (self.rho * km + np.sqrt(km ** 2 + self.nu ** 2))

    def dtotal_var_dk(self, k: np.ndarray) -> np.ndarray:
        """First derivative dw/dk."""
        km = k - self.m
        return self.b * (self.rho + km / np.sqrt(km ** 2 + self.nu ** 2))

    def d2total_var_dk2(self, k: np.ndarray) -> np.ndarray:
        """Second derivative d²w/dk²."""
        km = k - self.m
        return self.b * self.nu ** 2 / (km ** 2 + self.nu ** 2) ** 1.5

    def iv(self, k: np.ndarray, t: float) -> np.ndarray:
        """Implied vol (decimal) from log-moneyness array and time-to-expiry T."""
        w = np.maximum(self.total_var(k), 0.0)
        return np.sqrt(w / max(t, 1e-12))

    # ── arbitrage checks ──────────────────────────────────────────────────

    def durrleman_g(self, k: np.ndarray) -> np.ndarray:
        """Durrleman's condition g(k).  Must be ≥ 0 for no butterfly arbitrage.

        g(k) = (1 - k·w'/(2w))² - (w')²/4·(1/w + 1/4) + w''/2
        """
        w = np.maximum(self.total_var(k), 1e-12)
        wp = self.dtotal_var_dk(k)
        wpp = self.d2total_var_dk2(k)
        return (1.0 - k * wp / (2.0 * w)) ** 2 - wp ** 2 / 4.0 * (1.0 / w + 0.25) + wpp / 2.0

    def has_butterfly_arbitrage(
        self, k_lo: float = -3.0, k_hi: float = 3.0, n: int = 400
    ) -> bool:
        """True if any butterfly arbitrage is detected on [k_lo, k_hi]."""
        g = self.durrleman_g(np.linspace(k_lo, k_hi, n))
        return bool(np.any(g < -1e-6))

    def min_var_ok(self) -> bool:
        """Non-negative minimum variance: a + b·ν·√(1-ρ²) ≥ 0."""
        return (
            self.a + self.b * self.nu * np.sqrt(max(1.0 - self.rho ** 2, 0.0)) >= -1e-8
        )

    def lee_ok(self) -> bool:
        """Lee moment formula: b·(1+|ρ|) ≤ 2."""
        return self.b * (1.0 + abs(self.rho)) <= 2.0 + 1e-8


# ── SVI fitter ────────────────────────────────────────────────────────────────

def _fit_svi(
    k_vals: np.ndarray,
    w_vals: np.ndarray,
) -> tuple[SVIParams | None, float]:
    """Fit SVI parameters to (log-moneyness, total-variance) observations.

    Uses SLSQP with explicit no-arbitrage constraints and multiple starting
    points to avoid local optima.  Falls back to unconstrained L-BFGS-B
    if SLSQP finds nothing feasible.

    Returns ``(params, rmse)`` or ``(None, inf)`` on failure.
    """
    n = len(k_vals)
    if n < 3:
        return None, float("inf")

    def svi_w(theta: np.ndarray) -> np.ndarray:
        a, b, rho, m, nu = theta
        km = k_vals - m
        return a + b * (rho * km + np.sqrt(km ** 2 + nu ** 2))

    def obj(theta: np.ndarray) -> float:
        return float(np.sum((svi_w(theta) - w_vals) ** 2))

    constraints = [
        # (1) non-negative minimum variance
        {
            "type": "ineq",
            "fun": lambda t: t[0] + t[1] * t[4] * np.sqrt(max(1.0 - t[2] ** 2, 0.0)),
        },
        # (2) Lee moment formula
        {
            "type": "ineq",
            "fun": lambda t: 2.0 - t[1] * (1.0 + abs(t[2])),
        },
    ]

    bounds = [
        (-0.5, 2.0),      # a
        (1e-5, 2.0),      # b
        (-0.999, 0.999),  # rho
        (-2.5, 2.5),      # m
        (1e-5, 2.0),      # nu
    ]

    w_mean = float(np.mean(w_vals))
    w_min = float(np.min(w_vals))

    # Multiple starting guesses biased toward BTC's typical skew
    x0s = [
        [w_mean * 0.90, 0.10, -0.40,  0.00, 0.20],
        [w_min  * 0.80, 0.20, -0.60, -0.10, 0.10],
        [w_mean * 0.80, 0.05, -0.20,  0.00, 0.30],
        [w_mean,        0.15,  0.00,  0.00, 0.10],
        [w_min,         0.30, -0.70, -0.20, 0.05],
    ]

    best_x: np.ndarray | None = None
    best_f = float("inf")

    for x0 in x0s:
        try:
            res = minimize(
                obj, x0,
                method="SLSQP",
                bounds=bounds,
                constraints=constraints,
                options={"maxiter": 500, "ftol": 1e-12},
            )
            if res.success and res.fun < best_f:
                best_x = res.x
                best_f = res.fun
        except Exception:
            continue

    # Fallback: unconstrained L-BFGS-B
    if best_x is None:
        for x0 in x0s[:2]:
            try:
                res = minimize(obj, x0, method="L-BFGS-B", bounds=bounds)
                if res.fun < best_f:
                    best_x = res.x
                    best_f = res.fun
            except Exception:
                continue

    if best_x is None:
        return None, float("inf")

    a, b, rho, m, nu = best_x
    params = SVIParams(
        a=float(a), b=float(b), rho=float(rho), m=float(m), nu=float(nu)
    )
    rmse = float(np.sqrt(best_f / n))
    return params, rmse


# ── Vol smile (single expiry) ─────────────────────────────────────────────────

class VolSmile:
    """Fitted SVI smile for one expiry slice.

    Holds the latest raw tick data, the fitted SVIParams, and a simple
    estimate of the market bid/ask IV half-spread for use by the digital
    pricer.
    """

    def __init__(
        self, expiry: datetime, forward: float, time_to_expiry: float
    ) -> None:
        self.expiry = expiry
        self.forward = forward            # F in USD (spot/index price, r≈0)
        self.time_to_expiry = time_to_expiry  # T in years

        self.params: SVIParams | None = None
        self.fit_rmse: float = float("inf")
        self.n_options: int = 0
        self.last_updated: datetime = datetime.now(timezone.utc)

        # Estimated half-spread in IV (decimal); default 2 vol-points
        self._iv_half_spread: float = 0.02

    # ── public ───────────────────────────────────────────────────────────

    def update(self, ticks: list[OptionTick]) -> None:
        """Refit SVI from the latest tick snapshot."""
        data = self._extract_vol_data(ticks)
        self.n_options = len(data)

        if len(data) < _MIN_FIT_POINTS:
            if data:
                # Flat vol fallback: use median mark IV
                iv_med = float(np.median([d[1] for d in data]))
                w_med = iv_med ** 2 * self.time_to_expiry
                self.params = SVIParams(a=w_med, b=1e-6, rho=0.0, m=0.0, nu=0.1)
            self.last_updated = datetime.now(timezone.utc)
            return

        k_arr = np.array([d[0] for d in data])
        w_arr = np.array([d[1] ** 2 * self.time_to_expiry for d in data])

        params, rmse = _fit_svi(k_arr, w_arr)
        if params is not None:
            self.params = params
            self.fit_rmse = rmse

        self._update_iv_spread(ticks)
        self.last_updated = datetime.now(timezone.utc)

    def iv(self, strike: float) -> float | None:
        """Mid implied vol (decimal) at a given strike."""
        if self.params is None or self.forward <= 0 or self.time_to_expiry <= 0:
            return None
        k = np.log(strike / self.forward)
        val = self.params.iv(np.array([k]), self.time_to_expiry)[0]
        return float(val) if np.isfinite(val) and val > 0 else None

    def iv_bid_ask(self, strike: float) -> tuple[float, float] | None:
        """(bid_iv, ask_iv) estimate at a given strike."""
        mid = self.iv(strike)
        if mid is None:
            return None
        bid = max(1e-4, mid - self._iv_half_spread)
        ask = mid + self._iv_half_spread
        return bid, ask

    # ── private ───────────────────────────────────────────────────────────

    def _extract_vol_data(
        self, ticks: list[OptionTick]
    ) -> list[tuple[float, float]]:
        """Return valid (log-moneyness, mid_iv_decimal) pairs from tick list."""
        data: list[tuple[float, float]] = []
        for tick in ticks:
            iv_pct: float | None = None
            if tick.mark_iv is not None and tick.mark_iv > 0:
                iv_pct = tick.mark_iv
            elif tick.bid_iv is not None and tick.ask_iv is not None:
                iv_pct = (tick.bid_iv + tick.ask_iv) / 2.0

            if iv_pct is None or not (1.0 <= iv_pct <= 500.0):
                continue

            iv = iv_pct / 100.0
            k = np.log(tick.strike / self.forward)
            if np.isfinite(k):
                data.append((float(k), float(iv)))
        return data

    def _update_iv_spread(self, ticks: list[OptionTick]) -> None:
        """Estimate a representative bid/ask half-spread in IV terms."""
        spreads = [
            (t.ask_iv - t.bid_iv) / 200.0  # pct → decimal, then halve
            for t in ticks
            if t.bid_iv is not None and t.ask_iv is not None and t.ask_iv > t.bid_iv
        ]
        if spreads:
            self._iv_half_spread = float(np.median(spreads))


# ── Vol surface ───────────────────────────────────────────────────────────────

class VolSurface:
    """Full implied vol surface — a collection of per-expiry SVI smiles.

    Usage::

        surface = VolSurface()
        updated_expiries = surface.update(tick_list)
        iv = surface.iv(strike=62_000, expiry=some_dt)   # float | None
        smile = surface.get_smile(expiry=some_dt)         # VolSmile | None
    """

    def __init__(self) -> None:
        self._ticks: dict[str, OptionTick] = {}          # instrument → latest tick
        self._smiles: dict[datetime, VolSmile] = {}      # expiry → fitted smile

    # ── public ───────────────────────────────────────────────────────────

    def update(
        self, ticks: list[OptionTick], now: datetime | None = None,
    ) -> set[datetime]:
        """Ingest new ticks and refit affected expiry slices.

        Only refits a slice when the incoming mark IV differs from the
        cached value, so repeated identical updates are cheap.

        ``now`` is the "as-of" instant the per-slice time-to-expiry is
        measured against.  It defaults to wall-clock (live behaviour
        unchanged), but the replay path passes the SimulatedClock's sim-time
        so the SVI fit -- and therefore every derived probability/edge -- is
        a deterministic function of the recorded surface (the same recorded
        frames + same sim-time yield bit-identical fits run-to-run), not of
        the wall-clock instant the replay happens to run at.

        Returns the set of expiry datetimes that were actually refitted.
        """
        dirty: set[datetime] = set()
        for tick in ticks:
            prev = self._ticks.get(tick.instrument_name)
            self._ticks[tick.instrument_name] = tick
            if prev is None or prev.mark_iv != tick.mark_iv:
                dirty.add(tick.expiry)

        for expiry in dirty:
            self._refit_expiry(expiry, now=now)

        return dirty

    def iv(self, strike: float, expiry: datetime) -> float | None:
        """Implied vol for (strike, expiry); interpolates the term structure."""
        smile = self._smiles.get(expiry)
        if smile is not None and smile.params is not None:
            return smile.iv(strike)
        return self._interpolate_iv(strike, expiry)

    def get_smile(self, expiry: datetime) -> VolSmile | None:
        return self._smiles.get(expiry)

    def forward_for_expiry(self, expiry: datetime) -> float | None:
        smile = self._smiles.get(expiry)
        return smile.forward if smile is not None else None

    def all_expiries(self) -> list[datetime]:
        """All expiry datetimes that have a fitted smile, sorted ascending."""
        return sorted(e for e, s in self._smiles.items() if s.params is not None)

    # ── private ───────────────────────────────────────────────────────────

    def _refit_expiry(self, expiry: datetime, now: datetime | None = None) -> None:
        expiry_ticks = [t for t in self._ticks.values() if t.expiry == expiry]
        if not expiry_ticks:
            return

        if now is None:
            now = datetime.now(timezone.utc)
        T = (expiry - now).total_seconds() / _SECONDS_PER_YEAR
        if T <= 0:
            return

        latest = max(expiry_ticks, key=lambda t: t.timestamp)
        forward = latest.underlying_price
        if forward <= 0:
            return

        smile = self._smiles.get(expiry)
        if smile is None:
            smile = VolSmile(expiry=expiry, forward=forward, time_to_expiry=T)
            self._smiles[expiry] = smile
        else:
            smile.forward = forward
            smile.time_to_expiry = T

        smile.update(expiry_ticks)
        logger.debug(
            "vol_smile_updated",
            expiry=expiry.isoformat(),
            n_options=smile.n_options,
            rmse=round(smile.fit_rmse, 6),
        )

    def _interpolate_iv(self, strike: float, expiry: datetime) -> float | None:
        """Interpolate IV in total-variance space between adjacent slices."""
        now = datetime.now(timezone.utc)
        T_q = (expiry - now).total_seconds() / _SECONDS_PER_YEAR
        if T_q <= 0:
            return None

        fitted = [
            ((e - now).total_seconds() / _SECONDS_PER_YEAR, s)
            for e, s in self._smiles.items()
            if s.params is not None and s.time_to_expiry > 0
        ]
        if not fitted:
            return None
        fitted.sort(key=lambda x: x[0])

        lower: tuple[float, VolSmile] | None = None
        upper: tuple[float, VolSmile] | None = None
        for T_e, smile in fitted:
            if T_e <= T_q:
                lower = (T_e, smile)
            else:
                upper = (T_e, smile)
                break

        if lower is None:
            return upper[1].iv(strike) if upper else None
        if upper is None:
            return lower[1].iv(strike)

        T_lo, s_lo = lower
        T_hi, s_hi = upper
        iv_lo = s_lo.iv(strike)
        iv_hi = s_hi.iv(strike)
        if iv_lo is None or iv_hi is None:
            return iv_lo or iv_hi

        # Linear interpolation in total variance (preserves calendar spread no-arb)
        w_lo = iv_lo ** 2 * T_lo
        w_hi = iv_hi ** 2 * T_hi
        frac = (T_q - T_lo) / max(T_hi - T_lo, 1e-10)
        w_q = w_lo + (w_hi - w_lo) * frac
        if w_q <= 0:
            return None
        return float(np.sqrt(w_q / T_q))
