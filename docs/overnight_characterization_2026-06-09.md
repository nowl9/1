# Overnight characterization: banked windows 2026-05-30 / 2026-06-01 / 2026-06-09

Date: 2026-06-10 (overnight sweep)
Baseline: master @ 4c862d8 (post no-arb-shadow); sweep plan + execution log
in docs/diag_overnight_sweep.md (commits 3bf62c9, c0e5a4d).
Method: each banked window replayed through the bare CLI
(`py -3.12 src/btc_pm_arb/main.py --mode replay --replay-date W`,
MIN_EDGE=0.03 from .env) into a FRESH `./paper_ledger`, analyzed with the
merged analyzer (`py -3.12 -m tools.analyze_paper_ledger`, per-window
`--out-dir`), then archived to
`ledger_archive/overnight_sweep_2026-06-09/<W>/` before the next window --
no cross-run union anywhere (each archived ledger carries exactly one
run_id). Fit-level no-arb rates from a gitignored observe-only probe
(`analysis_out/_sweep_noarb_fitrate.py`) wrapping `noarb_check_by_strike`
during a second replay pass per window. data/recordings READ-ONLY
throughout (newest recording mtime 2026-06-09 15:12, before the sweep).
Zero source changes; no floor / fill-model / shadow-layer behavior touched.

## Per-window comparison

| | 2026-05-30 | 2026-06-01 | 2026-06-09 |
|---|---|---|---|
| recorded hours (UTC) | 18, 20 | 18-20 (h20 truncated) | 18-19 (h18 truncated) |
| frames replayed | 42,109 | 702,528 | 244,805 |
| vol regime | quiet (low/normal osc., max rv_1h 0.0046) | quiet (low/normal osc., max rv_1h 0.3603) | quiet (low/normal osc., max rv_1h 0.383) |
| emitted orders | 0 | 1 (buy_no, settled WIN +24.70) | 0 |
| order fill_adjusted_edge (#1 on passers) | - | -0.1496 (adjusted_edge +0.0304) | - |
| rejections total | 292 | 2,345 | 509 |
| .. range_product | 108 | 726 | 203 |
| .. no_positive_edge | 8 | 786 | 90 |
| .. conservative_edge | 79 | 531 | 131 |
| .. one_touch_barrier | 97 | 302 | 45 |
| .. days_to_expiry | 0 | 0 | 40 |
| #2 capped band: walked (full/partial/no-fill) | 45 (19/26/0) | 284 (89/195/0) | 126 (70/51/5) |
| #2 mean fill-adj in [1,3%) | +0.0195 (n=43) | +0.0201 (n=284) | +0.0150 (n=86) |
| #1 chase band: negative / non-neg | 45 / 0 | 284 / 0 | 126 / 0 |
| #1 mean chase, worst bucket (<-5%) | -0.755 (n=43) | -0.143 (n=133) | -0.371 (n=121) |
| noarb_shadow records (edge-level) | 0 | 151 | 0 |
| .. reasons | - | calendar_vs_prev 151 | - |
| .. with positive would-be edge | - | 151 (100%) | - |
| .. signal_emitted=true | - | 0 | - |
| UNIQUE fits violating / seen (fit-level) | 75/377 = 19.9% | 1319/5138 = 25.7% | 362/981 = 36.9% |
| strike-points flagged / checked | 477/13,118 (3.6%) | 20,935/192,911 (10.9%) | 4,624/42,427 (10.9%) |
| fit-level butterfly violations | 0 | 0 | 0 |

Sources: per-window `analyzer.log` / `fill_adjusted_band.json` /
`paper_ledger/*.jsonl` and `sweep_captures.json` under
`ledger_archive/overnight_sweep_2026-06-09/`; fit-level numbers in
`noarb_fitrate.json` (same dir).

## Cross-window read

**The ~25.7% calendar no-arb rate on 0601 is not a window artifact -- it
is the MIDDLE of the cross-window range.** Fit-level static-arb violation
rates are 19.9% (0530), 25.7% (0601 -- reproducing docs/diag_noarb.md
section 4 EXACTLY: 1319/5138 unique fits, 20,935/192,911 strike-points,
numerically identical to the diag's per-pricing-call counts), and
36.9% (0609). One-in-five to one-in-three unique SVI fits in every
banked quiet window violates static no-arb at the digital's strikes. The
figure that previously had solo verification now has three independent
window measurements behind it.

**The failure mode is calendar-only everywhere.** Zero butterfly
violations across all three windows (and zero min_var/Lee by
construction -- SLSQP constrains those). 0530 and 0609 also show the
crossing from the shorter slice's side (`calendar_vs_next` 112 and 357
strike-points respectively); 0601's flagged points were all
`calendar_vs_prev`. Same crossing, viewed from whichever adjacent slice
was being priced.

**Fit-level violations are pervasive, but edge-level association is
window-dependent.** Despite 19.9%/36.9% fit-level rates, 0530 and 0609
produced ZERO noarb_shadow records: no computed edge consumed a flagged
(strike, expiry) cache entry in those windows (their violating fits sat
on strikes/expiries the matcher never matched). Only 0601 materialized
the association: 151 edge-level records, 100% carrying positive would-be
edge (max +0.96 deep-ITM artifact class), 100% caught by existing gates
(`signal_emitted=false` on all). The shadow layer's value is exactly
this association, and the quiet-window baseline for the upcoming
quiet-vs-CPI comparison is now three windows wide at the fit level.

**The 3% floor's rejections are real losers under completion-cost
semantics -- in every window.** The #1 UNCAPPED chase band is 100%
negative on all walked rejections (45/45, 284/284, 126/126): zero
would-be winners were rejected. Mean chase cost in the worst bucket
varies with book depth -- -75.5% on 0530's thin books, -14.3% on 0601,
-37.1% on 0609 -- but the sign never flips. No clipping was applied
anywhere; negatives are reported as-is.

**The #2 CAPPED band confirms near-floor collapse shows as partial-fill
rate, not negative edge.** Mean #2 fill-adjusted edge equals mean
theoretical edge in every bucket of every window (the cap binds);
thinness appears instead as partials: 26/45 (58%) on 0530, 195/284 (69%)
on 0601, 51/126 (40%, plus 5 no-fills) on 0609.

**Order economics at the 3% floor remain regime-consistent.** Zero
orders on 0530/0609 and exactly one on 0601 (buy_no @ 0.81, adjusted
edge +3.04%, persisted #1 fill_adjusted_edge -14.96%, settled WIN
+24.70). The quiet-regime banked windows simply do not offer terminal
edge above the prod floor -- consistent with the
replay-2026-06-01-zero-signal diagnosis; the floor itself was not
touched by this sweep.

**Data properties noted (not defects introduced by this sweep):** the
0601 hour-20 and 0609 hour-18 deribit+polymarket captures are truncated
mid-gzip; the reader logs `replay.truncated_recording` and stops that
stream deterministically (identical counts in the banked sweep and the
probe pass). 0530 has no hour-19 capture. Kalshi depth lists remain
unpopulated in `_build_tick` (known latent defect, out of scope here).
Kalshi DID form matches on 0530 -- 53 of its 292 rejections are
platform=kalshi, all `one_touch_barrier` (a post-match product-type
filter) -- and was correctly excluded there; on 0601 and 0609 every
rejection is Polymarket (Kalshi formed no matches in those windows).

## Isolation evidence (the hazard this sweep had to avoid)

- Pre-sweep: leftover live ledger moved to
  `ledger_archive/paper_ledger_pre_sweep_20260610-001244`.
- `./paper_ledger` verified ABSENT before each replay and after each
  archive move; it does not exist at sweep end.
- Every record in each archived ledger carries exactly one run_id
  (0530: ac18e918..., 292 records; 0601: bb1c6d87..., 2,500 records =
  2,345 rejections + 151 noarb_shadow + 1 each order/intent/fill/
  settlement; 0609: 24c94284..., 509 records) -- independently
  recounted across all six jsonl streams during verification.
- The 0601 analyzer scope line pinned its own run: 1/1 orders in scope.
  0530/0609 report "all runs" scope only because zero orders means
  `latest_run_id` is None -- harmless on a single-run dir.
- Cross-validation: 0601 reproduced every previously-banked figure
  (frames, walked counts 89/195, +2.01% band mean, 151 shadow records,
  -0.1496 order fill_adjusted_edge).

## Verification

- `py -3.12 -m pytest -q`: 841 passed (run at Phase 1 gate, Phase 2
  gate, and Phase 3 gate -- see commit messages).
- `git status` clean at the final commit; numstat across the sweep
  commits shows docs/ additions only, zero source lines changed
  (ledger/analysis artifacts live under gitignored `ledger_archive/`
  and `analysis_out/`).
- Out-of-scope guardrails held: no source change to main/analyzer/
  pricing/filters, no new CLI flags or persistence, no edge-floor or
  fill-model change, no clipping of #1 negatives, shadow layer still
  observe-only, recorder/--record-feeds path untouched.
