# Diagnosis: static no-arb violations at the digital pricing site (Phase 1)

Date: 2026-06-09
Baseline: master @ c54fe50, 822 tests green, tree clean.
Goal under diagnosis: SHADOW-mode no-arb validation for the SVI pricing
engine -- detect surfaces that violate static no-arb (butterfly /
calendar) at the digital's strikes and LOG every would_reject, while
emitting every signal UNCHANGED.

## VERDICT: HARD GATE PASSES -- real violations occur, proceed to Phase 2

On the banked 2026-06-01 window (hours 18-20, min_edge=0.03), 1319 of
5138 unique SVI fits (25.7%) violate static no-arb at the digital's
strikes, all of them CALENDAR violations (total implied variance at the
strike decreasing from the adjacent shorter slice). 151 of 2363
computed edges (6.4%) consumed a cache entry whose latest fit was in a
violating state -- and ALL 151 carried a positive would-be edge,
including absurd +0.96 conservative edges on a deep-ITM digital priced
off a far-dated slice. Zero butterfly violations fired in this window.
Full numbers in section 4.

## 1. Where the digital is produced and which strikes/expiries are used

Producer: `btc_pm_arb.pricing.digital_pricer:DigitalPricer.price_from_surface`
(src/btc_pm_arb/pricing/digital_pricer.py:101). Analytical BS digital
N(d2) using the SVI smile vol at the digital's strike:

| input | source | location |
|-------|--------|----------|
| smile | `surface.get_smile(expiry)` | digital_pricer.py:112 |
| F, T | `smile.forward`, `smile.time_to_expiry` | digital_pricer.py:116-117 |
| iv_mid | `smile.iv(strike)` -> `SVIParams.iv(k=ln(K/F), T)` | digital_pricer.py:121, vol_surface.py:265-271 |
| iv bid/ask | `smile.iv_bid_ask(strike)` (mid +/- half-spread) | digital_pricer.py:125, vol_surface.py:273-280 |

The ONLY strike this method evaluates the surface at is the digital's
own strike K (log-moneyness k = ln(K/F) of the fitted slice). The
call-spread variant `price_from_ticks` (digital_pricer.py:141), which
would use K-delta / K+delta, has ZERO call sites in src/ -- the
analytical surface path is the only live digital producer (verified by
grep; only the definition matches).

Call site (the digital pricing site): `Agent.update_cache_from_surface`
(src/btc_pm_arb/main.py:420), loop body at main.py:430-431 -- for each
dirty expiry it prices EVERY strike present in the tick store for that
expiry (`strikes` set built at main.py:425-429 from
`surface._ticks` where `mark_iv is not None`). So strikes/expiries are
known at main.py:425-431; the per-call (strike, expiry) pair is exactly
the digital being priced.

Downstream (where contract id + would-be edge first co-exist):
cache write `ProbabilityCache.update` (main.py:434-445) -> matcher
(main.py:476) -> `EdgeCalculator.compute` (main.py:484, edge.py:104).
At price_from_surface time there is NO contract id -- a Phase 2 shadow
record that must carry (contract id, reasons, would-be edge) has to
flag the violation at the pricing site and emit the record at the edge
step, where `match.matched_strike` / `match.matched_expiry` identify
the consumed cache entry (edge.py:234-244 uses the same pair).

## 2. Why violating fits are possible at all

The SVI fitter `_fit_svi` (src/btc_pm_arb/pricing/vol_surface.py:116)
constrains only:

  (1) non-negative minimum variance (vol_surface.py:140-145)
  (2) Lee moment bound b(1+|rho|) <= 2 (vol_surface.py:146-150)

Durrleman's g(k) >= 0 (butterfly / non-negative density) is NOT a fit
constraint, and calendar consistency across slices is never checked
anywhere (each expiry slice is fit independently,
vol_surface.py:385-409). Two fit paths are entirely unconstrained:

  - L-BFGS-B fallback when SLSQP finds nothing feasible
    (vol_surface.py:191-200) -- bounds only, no arb constraints;
  - flat-vol fallback when < 5 points (vol_surface.py:245-250).

`SVIParams` already carries the check primitives -- `durrleman_g`
(vol_surface.py:86), `has_butterfly_arbitrage` (:96), `min_var_ok`
(:103), `lee_ok` (:109) -- but NOTHING in src/ calls them (grep:
definitions only). The detector exists; it is simply not wired.

## 3. Probe method (throwaway, deleted)

Monkeypatched `DigitalPricer.price_from_surface` during a banked
2026-06-01 replay (hours 18-20, `min_edge=0.03`, SimulatedClock,
jump_to_expiry -- same harness as prior banked diagnostics). At every
call that returned a DigitalPrice, checked the fitted slice at the
digital's strike:

  - butterfly_g: Durrleman g(ln(K/F)) < -1e-6 (same tolerance as
    `has_butterfly_arbitrage`); g >= 0 IS call-price convexity in
    strike / non-negative implied density for an SVI slice;
  - min_var / lee: the two static SVIParams sanity bounds;
  - calendar_vs_prev / calendar_vs_next: total implied variance at the
    strike, w_i(ln(K/F_i)) via `SVIParams.total_var`, strictly
    decreasing (> 1e-9) from the adjacent earlier fitted slice to this
    one, or from this one to the adjacent later slice.

Also wrapped `EdgeCalculator.compute` to count edges computed while the
latest fit for the consumed (matched_strike, matched_expiry) cache
entry was in a violating state -- the would-be-artifact edges.

## 4. FINDINGS (probe output)

Replay stats: 702528 frames, 689188 deribit ticks, 5744 pm ticks,
6011 kalshi ticks, 1 settlement. (Known data property: the hour-20
deribit/polymarket recordings are truncated mid-gzip -- the reader
logs `replay.truncated_recording` and stops that stream; deterministic
run-to-run, so before/after comparisons remain valid.)

Pricing-site checks:

| metric | count |
|--------|-------|
| pricing calls (DigitalPrice produced) | 192911 |
| pricing calls violating | 20935 (10.9%) |
| ... butterfly_g (Durrleman g < -1e-6 at strike) | 0 |
| ... min_var violated | 0 |
| ... lee violated | 0 |
| ... calendar_vs_prev (w decreasing into this slice) | 20935 |
| ... calendar_vs_next (w decreasing out of this slice) | 0 |
| unique fits seen | 5138 |
| unique fits violating | 1319 (25.7%) |

Violation rate is uniform across the window (10.8% / 11.0% / 10.8% of
calls in hours 18/19/20) -- no high-vol cluster INSIDE this quiet
window; the quiet-vs-CPI comparison is exactly what the shadow log is
for.

Magnitudes are far above fit noise: calendar decrease in total
variance has median 4.0e-2, max 2.9e-1; relative to the shorter
slice's w the median decrease is 39%, max 79.5%. These are real
crossed calendars between independently fitted slices, not epsilon
re-fit jitter.

Edge association (would-be-artifact edges):

| metric | count |
|--------|-------|
| edges computed | 2363 |
| edges on a violating fit | 151 (6.4%) |
| ... with positive would-be edge | 151 (100%) |

Sample association (from the capped probe sample): contract
775884...1184, strike 63000, expiry 2026-06-05, buy_yes,
best_conservative_edge +0.961, fill_adjusted_edge +0.955 -- a deep-ITM
digital (F ~ 72211) whose 96% "edge" sits on a calendar-violating fit.

Representative violating fit (for hand-built test fixtures): expiry
2026-09-25 slice with params (a=-0.01262, b=0.14619, rho=-0.32152,
m=-0.01122, nu=0.38895), F=72211.1, T=0.3164: at K=320000 (k=1.489)
its w=0.1434 sits BELOW the adjacent shorter 2026-08-28 slice's
w=0.2447 at the same strike -- a 41% total-variance drop moving OUT in
expiry. g(k)=+0.246 there (butterfly clean), confirming calendar and
butterfly are independent failure modes.

## 5. Phase 2 implications

- The live failure mode in banked data is CALENDAR-only. Butterfly
  never fired here (the SLSQP min-var/Lee constraints plus parameter
  bounds appear to keep single slices Durrleman-clean in this regime),
  but the goal's detector covers butterfly + calendar: the butterfly
  check is a handful of scalar ops and high-vol regimes (CPI) may
  exercise it where this quiet window did not.
- Wiring must be two-step: `noarb_check` runs at the digital pricing
  site (`update_cache_from_surface` loop, main.py:430-431) and flags
  (strike, expiry); the JSONL record is emitted at the edge step in
  `run_scan_pipeline`, the first point where contract id and the
  would-be edge co-exist (probe used the same association and it is
  exact: the latest pricing call for a pair is the one that wrote the
  cache entry the matcher consumes). Matcher nuance verified:
  `MatchResult.matched_strike` is always an exact cache GRID strike
  (matcher.py:135,174) -- i.e. precisely a (strike, expiry) the pricing
  site flagged -- and although `options_entry` is strike-interpolated
  (matcher.py:152), both bracketing grid entries are written by the
  SAME per-expiry fit generation (`update_cache_from_surface` re-prices
  every strike of a dirty expiry in one loop off one fit), so the
  nearest-grid-strike flag is the right slice-level association.
- Tolerances pinned by the probe: butterfly at g < -1e-6 (identical to
  `SVIParams.has_butterfly_arbitrage`); calendar at any decrease
  > 1e-9 in total variance (real violations are ~7 orders of magnitude
  above this; the record carries the magnitude so the LATER
  suppression goal can pick a threshold from shadow data).
- JSONL via the established paper_ledger append pattern (run_id/mode
  stamped, fsync'd, replay_* reader for tests) into a new
  `noarb_shadow.jsonl` stream.
- SHADOW ONLY: no suppression, no DigitalPrice change, no signal
  change; the determinism regression (before/after banked replay,
  identical emitted signals and fill-adjusted edges) is the Phase 3
  proof.
