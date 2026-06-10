"""Unit tests for pricing/noarb.py -- the pure static no-arb checks.

Covers the no-arb shadow goal's Phase 3 unit matrix: a clean surface
checks ok; a hand-built butterfly-violating smile (Axel Vogt's classic
SVI parameters, g(k) < 0 on k in ~[0.65, 1.25]) and a hand-built
calendar-violating pair (longer slice with LOWER total variance) each
produce the right reason at the right strike.  Plus the degenerate /
boundary cases: unfitted slice, bad forward, no strikes, sub-tolerance
calendar jitter.

The function is pure and read-only -- no Agent, no cache, no pricing.
Pipeline-level shadow behavior is covered in
tests/test_main/test_noarb_shadow.py.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from btc_pm_arb.pricing.noarb import (
    CAL_TOL,
    noarb_check,
    noarb_check_by_strike,
)
from btc_pm_arb.pricing.vol_surface import SVIParams, VolSmile, VolSurface

_F = 100_000.0
_E1 = datetime(2026, 6, 16, 8, tzinfo=timezone.utc)   # shorter expiry
_E2 = datetime(2026, 6, 23, 8, tzinfo=timezone.utc)   # longer expiry

# Axel Vogt's classic butterfly-arbitrageable raw-SVI parameters:
# Durrleman g(k) < 0 for k in ~[0.65, 1.25] (min g ~ -0.0327 at k=0.9)
# while the smile stays clean at ATM (g(0) ~ +1.04).
_VOGT = SVIParams(a=-0.0410, b=0.1331, rho=0.3060, m=0.3586, nu=0.4153)
_K_BAD = _F * math.exp(0.9)

# Flat-ish slices for calendar scenarios: w(k) ~ a everywhere.
_FLAT_LO = SVIParams(a=0.01, b=1e-3, rho=0.0, m=0.0, nu=0.1)
_FLAT_HI = SVIParams(a=0.02, b=1e-3, rho=0.0, m=0.0, nu=0.1)


def _make_smile(expiry: datetime, T: float, params: SVIParams | None) -> VolSmile:
    smile = VolSmile(expiry=expiry, forward=_F, time_to_expiry=T)
    smile.params = params
    return smile


def _surface(smiles: dict[datetime, VolSmile]) -> VolSurface:
    surf = VolSurface()
    surf._smiles.update(smiles)
    return surf


class TestCleanSurface:
    def test_increasing_total_variance_is_ok_on_both_slices(self) -> None:
        surf = _surface({
            _E1: _make_smile(_E1, 7 / 365.25, _FLAT_LO),
            _E2: _make_smile(_E2, 14 / 365.25, _FLAT_HI),
        })
        strikes = [90_000.0, _F, 110_000.0]
        assert noarb_check(surf, strikes, _E1) == (True, [])
        assert noarb_check(surf, strikes, _E2) == (True, [])
        assert noarb_check_by_strike(surf, strikes, _E1) == {}

    def test_vogt_smile_is_clean_at_atm(self) -> None:
        # The butterfly violation is strike-local: the same arbitrageable
        # smile checks ok at a strike outside the g<0 region.
        surf = _surface({_E2: _make_smile(_E2, 30 / 365.25, _VOGT)})
        ok, reasons = noarb_check(surf, [_F], _E2)
        assert ok and reasons == []


class TestButterfly:
    def test_vogt_smile_flags_butterfly_at_bad_strike(self) -> None:
        surf = _surface({_E2: _make_smile(_E2, 30 / 365.25, _VOGT)})
        ok, reasons = noarb_check(surf, [_K_BAD], _E2)
        assert not ok
        assert len(reasons) == 1
        assert reasons[0].startswith("butterfly:g=-")
        assert reasons[0].endswith(f"@K={_K_BAD:g}")
        # Sanity: the reason reflects a genuinely negative Durrleman g.
        g = float(_VOGT.durrleman_g(np.array([0.9]))[0])
        assert g < -1e-6

    def test_by_strike_keys_only_violating_strikes(self) -> None:
        surf = _surface({_E2: _make_smile(_E2, 30 / 365.25, _VOGT)})
        by_strike = noarb_check_by_strike(surf, [_K_BAD, _F], _E2)
        assert set(by_strike) == {_K_BAD}
        assert by_strike[_K_BAD][0].startswith("butterfly:")


class TestCalendar:
    def test_crossed_pair_flags_prev_on_long_and_next_on_short(self) -> None:
        # Longer slice has LOWER total variance at the strike: the same
        # crossing is reported from whichever slice is being checked.
        surf = _surface({
            _E1: _make_smile(_E1, 7 / 365.25, _FLAT_HI),
            _E2: _make_smile(_E2, 14 / 365.25, _FLAT_LO),
        })
        ok_long, reasons_long = noarb_check(surf, [_F], _E2)
        assert not ok_long
        assert len(reasons_long) == 1
        assert reasons_long[0].startswith("calendar_vs_prev:w=")

        ok_short, reasons_short = noarb_check(surf, [_F], _E1)
        assert not ok_short
        assert len(reasons_short) == 1
        assert reasons_short[0].startswith("calendar_vs_next:w=")

    def test_reason_carries_both_w_values_for_thresholding(self) -> None:
        # The later suppression goal picks a materiality threshold from the
        # shadow data, so the record must carry the magnitude.
        surf = _surface({
            _E1: _make_smile(_E1, 7 / 365.25, _FLAT_HI),
            _E2: _make_smile(_E2, 14 / 365.25, _FLAT_LO),
        })
        _, reasons = noarb_check(surf, [_F], _E2)
        # "calendar_vs_prev:w=<w_short>-><w_long>@K=100000"
        body = reasons[0].split(":w=")[1].split("@")[0]
        w_short, w_long = (float(x) for x in body.split("->"))
        assert w_short > w_long
        assert math.isclose(w_short - w_long, 0.01, rel_tol=0.05)

    def test_sub_tolerance_jitter_is_not_flagged(self) -> None:
        # A decrease below CAL_TOL is refit noise, not a violation.
        lo = SVIParams(a=0.02, b=1e-3, rho=0.0, m=0.0, nu=0.1)
        hi = SVIParams(a=0.02 + CAL_TOL / 10.0, b=1e-3, rho=0.0, m=0.0, nu=0.1)
        surf = _surface({
            _E1: _make_smile(_E1, 7 / 365.25, hi),
            _E2: _make_smile(_E2, 14 / 365.25, lo),
        })
        assert noarb_check(surf, [_F], _E2) == (True, [])

    def test_single_slice_has_no_calendar_neighbours(self) -> None:
        surf = _surface({_E2: _make_smile(_E2, 14 / 365.25, _FLAT_LO)})
        assert noarb_check(surf, [_F], _E2) == (True, [])


class TestDegenerateInputs:
    def test_unfitted_smile_returns_ok(self) -> None:
        surf = _surface({_E2: _make_smile(_E2, 14 / 365.25, None)})
        assert noarb_check(surf, [_F], _E2) == (True, [])
        assert noarb_check_by_strike(surf, [_F], _E2) == {}

    def test_expiry_not_in_surface_returns_ok(self) -> None:
        surf = _surface({})
        assert noarb_check(surf, [_F], _E2) == (True, [])

    def test_nonpositive_forward_returns_ok(self) -> None:
        smile = _make_smile(_E2, 14 / 365.25, _FLAT_LO)
        smile.forward = 0.0
        surf = _surface({_E2: smile})
        assert noarb_check(surf, [_F], _E2) == (True, [])

    def test_no_valid_strikes_returns_ok(self) -> None:
        surf = _surface({_E2: _make_smile(_E2, 14 / 365.25, _FLAT_LO)})
        assert noarb_check(surf, [], _E2) == (True, [])
        assert noarb_check(surf, [-1.0, 0.0], _E2) == (True, [])

    def test_degenerate_neighbour_is_skipped_not_crashed(self) -> None:
        # A crossed pair where the SHORTER slice has a bad forward: the
        # calendar comparison silently skips it (nothing checkable).
        bad = _make_smile(_E1, 7 / 365.25, _FLAT_HI)
        bad.forward = -1.0
        surf = _surface({
            _E1: bad,
            _E2: _make_smile(_E2, 14 / 365.25, _FLAT_LO),
        })
        assert noarb_check(surf, [_F], _E2) == (True, [])

    def test_pure_no_surface_mutation(self) -> None:
        surf = _surface({
            _E1: _make_smile(_E1, 7 / 365.25, _FLAT_HI),
            _E2: _make_smile(_E2, 14 / 365.25, _FLAT_LO),
        })
        params_before = (surf._smiles[_E2].params.a, surf._smiles[_E1].params.a)
        noarb_check(surf, [_F, 90_000.0, 110_000.0], _E2)
        assert (surf._smiles[_E2].params.a, surf._smiles[_E1].params.a) == params_before
        assert surf._ticks == {}
