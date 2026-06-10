"""Static no-arbitrage checks at the digital's strikes -- SHADOW layer.

Pure, read-only validation of the fitted SVI surface at the exact
(strike, expiry) grid points the digital pricer evaluates
(``DigitalPricer.price_from_surface`` reads the smile ONLY at the
digital's own strike).  Two static conditions, per docs/diag_noarb.md:

  butterfly:  Durrleman g(k) >= 0 at the digital's log-moneyness
              k = ln(K/F) -- equivalent to call-price convexity in
              strike / non-negative implied density for an SVI slice.
              Tolerance g < -1e-6 matches
              ``SVIParams.has_butterfly_arbitrage``.

  calendar:   total implied variance at the strike non-decreasing in
              expiry across ADJACENT fitted slices:
              w_prev(ln(K/F_prev)) <= w_self(ln(K/F_self)) and
              w_self <= w_next(ln(K/F_next)).  Any decrease beyond
              1e-9 is a violation; Phase 1 measured real violations at
              median 4.0e-2 (39% relative), ~7 orders of magnitude
              above this floor.  The reason string carries both w
              values so the LATER suppression goal can pick a
              materiality threshold from shadow data.

A violating pair indicts the PAIR, not a slice: ``calendar_vs_prev``
on the longer slice and ``calendar_vs_next`` on the shorter slice are
the same crossing seen from whichever slice is being priced when the
check runs (directionality of "which fit is wrong" is unknowable
statically).

SHADOW ONLY: callers must not let the result alter any DigitalPrice,
cache entry, or emitted signal.  This layer exists to MEASURE how
often an arb-violating smile produces a digital probability that
looks like edge (the +0.96 conservative-edge artifact class found in
Phase 1) -- suppression is a separate, later goal gated on the
quiet-vs-CPI shadow comparison.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from btc_pm_arb.pricing.vol_surface import VolSmile, VolSurface

# Same threshold as SVIParams.has_butterfly_arbitrage.
G_TOL = -1e-6
# Decrease in total variance beyond this is a calendar violation.
CAL_TOL = 1e-9


def noarb_check(
    surface: VolSurface,
    strikes: list[float],
    expiry: datetime,
) -> tuple[bool, list[str]]:
    """Static no-arb verdict for one fitted expiry slice at the given strikes.

    Returns ``(ok, reasons)``: ``ok`` is True iff no strike violates
    butterfly or calendar no-arb; ``reasons`` carries one entry per
    violation, strike-tagged, e.g.
    ``"calendar_vs_prev:w=0.244693->0.143401@K=320000"``.

    Pure read-only: no surface, cache, or pricing state is mutated.
    """
    by_strike = noarb_check_by_strike(surface, strikes, expiry)
    reasons = [
        f"{r}@K={strike:g}"
        for strike, strike_reasons in sorted(by_strike.items())
        for r in strike_reasons
    ]
    return (not reasons), reasons


def noarb_check_by_strike(
    surface: VolSurface,
    strikes: list[float],
    expiry: datetime,
) -> dict[float, list[str]]:
    """Per-strike static no-arb reasons for one fitted expiry slice.

    The per-strike variant of :func:`noarb_check` -- same checks, keyed
    by strike so the caller can associate a violation with the exact
    (strike, expiry) grid point a cache entry / matched edge came from.
    Strikes with no violation are absent from the result; an unfitted
    or degenerate slice returns ``{}`` (nothing checkable, nothing
    flagged).  Pure read-only.
    """
    smile = surface.get_smile(expiry)
    if smile is None or smile.params is None:
        return {}
    forward = smile.forward
    if forward is None or forward <= 0:
        return {}

    valid = sorted(s for s in set(strikes) if s > 0)
    if not valid:
        return {}

    k_arr = np.log(np.array(valid) / forward)
    params = smile.params
    g_arr = params.durrleman_g(k_arr)
    w_self = params.total_var(k_arr)

    prev_smile, next_smile = _adjacent_fitted_smiles(surface, expiry)
    w_prev = _total_var_at_strikes(prev_smile, valid)
    w_next = _total_var_at_strikes(next_smile, valid)

    out: dict[float, list[str]] = {}
    for i, strike in enumerate(valid):
        reasons: list[str] = []
        g = float(g_arr[i])
        if np.isfinite(g) and g < G_TOL:
            reasons.append(f"butterfly:g={g:.6g}")
        ws = float(w_self[i])
        if w_prev is not None and ws < float(w_prev[i]) - CAL_TOL:
            reasons.append(f"calendar_vs_prev:w={float(w_prev[i]):.6g}->{ws:.6g}")
        if w_next is not None and float(w_next[i]) < ws - CAL_TOL:
            reasons.append(f"calendar_vs_next:w={ws:.6g}->{float(w_next[i]):.6g}")
        if reasons:
            out[strike] = reasons
    return out


def _adjacent_fitted_smiles(
    surface: VolSurface, expiry: datetime,
) -> tuple[VolSmile | None, VolSmile | None]:
    """The fitted smiles adjacent to ``expiry`` in the term structure.

    Uses ``VolSurface.all_expiries()`` (fitted slices only, sorted
    ascending).  Returns ``(prev, next)``; either is None at the ends
    of the term structure, when the expiry itself has no fitted smile,
    or when the neighbour is degenerate (no params / non-positive
    forward).
    """
    expiries = surface.all_expiries()
    try:
        idx = expiries.index(expiry)
    except ValueError:
        return None, None

    def usable(e: datetime | None) -> VolSmile | None:
        if e is None:
            return None
        s = surface.get_smile(e)
        if s is None or s.params is None or s.forward is None or s.forward <= 0:
            return None
        return s

    prev_e = expiries[idx - 1] if idx > 0 else None
    next_e = expiries[idx + 1] if idx + 1 < len(expiries) else None
    return usable(prev_e), usable(next_e)


def _total_var_at_strikes(
    smile: VolSmile | None, strikes: list[float],
) -> np.ndarray | None:
    """Total implied variance of ``smile`` at each strike (its own ln(K/F))."""
    if smile is None:
        return None
    k = np.log(np.array(strikes) / smile.forward)
    return smile.params.total_var(k)
