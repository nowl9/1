# C3: 2026-05-30 re-replay through the fixed Kalshi depth parser

Goal: Kalshi `_build_tick` depth defect, Phase 2 unit C3. Proves the C1 fix
(commit 6c2d5af) is replay-side-effective (RETROACTIVE verdict, accepted at
the Phase 1 gate) by re-replaying ONE banked window with Kalshi matches --
2026-05-30 -- through the merged analyzer into a fresh isolated ledger, and
comparing against the overnight-sweep baseline
(`ledger_archive/overnight_sweep_2026-06-09/2026-05-30/`).

Full re-characterization of the other windows is OUT OF SCOPE here
(separate post-FOMC goal).

## Pre-registered expectation (stated before results were seen)

1. Zero `reason_key` changes. All 53 Kalshi rejections in this window are
   `one_touch_barrier`, which fires BEFORE any book stage in the filter
   chain (`one_touch -> range -> no_edge -> empty_book -> edge floors`).
2. Movement in book-dependent fields only: populated `order_book_yes/no`
   on Kalshi ticks; shadow-walk fields non-null where applicable.
3. Determinism: all non-Kalshi rows identical in verdict fields.
4. If `reason_key` moves anywhere, that is a surprise -- stop and report,
   do not rationalize.

## Isolation discipline (same as the overnight sweep)

- Prior live `./paper_ledger` (capture-era rejections + noarb shadow, last
  write 2026-06-10 22:30 local, no python process running) archived to
  `ledger_archive/paper_ledger_pre_c3_20260610-232244/`.
- `./paper_ledger` verified ABSENT before the run.
- Replay: `py -3.12 -m btc_pm_arb.main --mode replay --replay-date
  2026-05-30`, exit 0, 2026-06-11T03:22:55Z -> 03:24:16Z.
- Analyzer: `py -3.12 -m tools.analyze_paper_ledger --ledger-dir
  paper_ledger --out-dir analysis_out_c3_0530`, exit 0 (0 orders in scope,
  rejections-only window -- identical shape to the sweep baseline).
- Single run_id verified across every jsonl stream in the fresh ledger:
  `7ef79389ebc5459f849a117b9aba95f0` (baseline sweep run was
  `ac18e918de21433794cdd919bda04f53`).
- Archived after: ledger + analysis_out + replay/analyzer logs at
  `ledger_archive/kalshi_depth_c3_2026-05-30/`.

## Results: before/after

Replay stream identity (recorded frames are immutable inputs):

| metric         | sweep baseline | C3 re-replay |
|----------------|---------------:|-------------:|
| frames         | 42,109         | 42,109       |
| deribit_ticks  | 40,981         | 40,981       |
| pm_ticks       | 508            | 508          |
| kalshi_ticks   | 443            | 443          |
| settlements    | 0              | 0            |

Ledger verdicts (rejections.jsonl, row-by-row aligned, 292 rows each):

| check                                            | result |
|--------------------------------------------------|--------|
| row identity (contract_id, platform) per row     | 292/292 match |
| reason_key changes                               | 0 (expected 0) |
| kalshi rows / reasons                            | 53, all one_touch_barrier (both runs) |
| kalshi verdict-field changes                     | 0 |
| non-kalshi verdict-field changes                 | 0 (expected 0) |
| kalshi book-dependent ledger fields non-null     | 0 (see below) |

Book-dependent movement (the point of the fix), visible at the tick layer:
the first-observed Kalshi tick `KXBTCMINMON-BTC-26MAY31-7000000`, which in
the sweep baseline log carried `"order_book_yes": [], "order_book_no": []`,
now carries 29 YES-side and 3 NO-side executable depth levels derived from
the SAME banked frames (level 0 agrees with top-of-book: yes_ask 0.04 ==
cheapest derived level 0.04). Phase 1 frame audit: 443/443 recorded
orderbook frames in this window carry multi-level depth, so every replayed
Kalshi tick now carries it too.

## Expectation verdict: HELD, including the null-fields prediction

The 53 Kalshi rejection rows keep every book-dependent ledger field null
because `one_touch_barrier` rejects before any book stage runs, and the
rejection shadow walkers (#1 chase / #2 shadow fill) only execute for the
near-floor in-band set, which contains no Kalshi rows in this window --
exactly as pre-registered at the Phase 1 gate ("expect before/after
movement in the book-dependent fields, not in reason_key", with the 53
verdicts unchanged). The window where Kalshi depth changes LEDGER numbers
is one with terminal Kalshi matches; that belongs to the post-FOMC
re-characterization.

What this run proves:

1. RETROACTIVE mechanics: the fixed `_build_tick` repopulates depth from
   banked raw frames with zero recording-side changes (recorder untouched,
   `data/recordings` read-only throughout).
2. No regression: byte-level verdict determinism on all 292 rows, zero
   reason_key movement, identical stream counts.
3. The FOMC Jun 16-17 tape is safe by construction: capture banks raw
   bodies; any replay after C1 parses them with full depth.
