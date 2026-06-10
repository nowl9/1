# Overnight characterization sweep -- Phase 1: resolved paths + isolation plan

Date: 2026-06-10
Baseline: master @ 4c862d8 (post no-arb-shadow), tree clean.
Goal: replay each banked window (2026-05-30, 2026-06-01, 2026-06-09) through
the merged analyzer in an ISOLATED per-window ledger, capture both fill bands
(#2 capped + #1 chase) and the no-arb shadow stream, and write one
consolidated comparison report (docs/overnight_characterization_2026-06-09.md).
READ-ONLY on data/recordings; zero source changes.

## 1. Resolved paths (verified against source at 4c862d8)

Replay WRITE path:
- The Agent constructs `PaperLedger(settings.paper_ledger_dir, ...)` at
  src/btc_pm_arb/main.py:249. `settings.paper_ledger_dir` defaults to
  `./paper_ledger` (src/btc_pm_arb/config.py:80). `.env` contains NO
  `PAPER_LEDGER_DIR` override (checked 2026-06-10), so the bare CLI replay
  appends to `./paper_ledger/{orders,intents,fills,settlements,rejections,
  noarb_shadow,risk_blocks}.jsonl`, each record stamped with the run's
  uuid `run_id` (PaperLedger._append, execution/paper_ledger.py:609).
- Replay READS recordings from `--record-dir` (default `data/recordings`),
  all hours present for the date, `jump_to_expiry=True`
  (feeds/replay.py:111-112 defaults; main.py:1683 passes neither).
- Replay NEVER writes recordings: the FrameRecorder is constructed only when
  `mode != "replay"` (main.py:1708-1710).

Analyzer READ path:
- `tools.analyze_paper_ledger` reads `--ledger-dir` (default `./paper_ledger`,
  analyze_paper_ledger.py:686-689): orders/fills/settlements/rejections.jsonl.
- Default scope = the LATEST run_id by order `created_at`
  (`latest_run_id`, analyze_paper_ledger.py:661-673); `--run-id all` opts out.
  This is the C3b defense against exactly the cross-run-union hazard this
  sweep must avoid.
- Outputs to `--out-dir` (default `./analysis_out`): report.md,
  summary_stats.json, joined.parquet, charts/, and fill_adjusted_band.json --
  the #2 CAPPED band with the #1 UNCAPPED chase band nested under "chase"
  (analyze_paper_ledger.py:841-849).
- `noarb_shadow.jsonl` is written by the replay (main.py:653, one record per
  edge whose consumed cache entry was flagged) but is NOT read by the
  analyzer; per-window counts and reasons breakdowns are computed by reading
  the archived stream directly (analysis-side, no source change).

## 2. Contamination hazard and the chosen isolation mechanism

Hazard: PaperLedger is append-only and `./paper_ledger` currently holds a
prior session's 2026-06-01 run (rejections.jsonl 492 KB, noarb_shadow.jsonl
15 KB). Replaying three windows into one dir would hand the analyzer a
cross-run union (the observed historical failure: 137 stale rows faked an
exposure breach).

Mechanism -- existing capabilities only, no new flags, no source changes:

1. Pre-sweep: `Move-Item ./paper_ledger ledger_archive/paper_ledger_pre_sweep_<stamp>`
   (same convention as the existing `ledger_archive/paper_ledger_20260602-140856`).
2. Per window W in {2026-05-30, 2026-06-01, 2026-06-09}, strictly sequential:
   a. Assert `./paper_ledger` does NOT exist (fresh start; PaperLedger
      creates the dir on first append).
   b. `py -3.12 src/btc_pm_arb/main.py --mode replay --replay-date W`
      -- bare defaults, identical to the original quiet read; stdout+stderr
      captured to the window's archive dir as replay.log.
   c. `py -3.12 -m tools.analyze_paper_ledger --out-dir ledger_archive/overnight_sweep_2026-06-09/W/analysis_out`
      -- `--ledger-dir` left at its default `./paper_ledger`, which at this
      point holds EXACTLY ONE run. `--out-dir` is an existing analyzer flag
      and changes only the artifact destination; running with the default
      `./analysis_out` would overwrite prior sessions' banked analysis
      artifacts, which this goal forbids ("nothing is overwritten").
      Analyzer stdout captured as analyzer.log (contains the scope line and
      both band tables).
   d. `Move-Item ./paper_ledger ledger_archive/overnight_sweep_2026-06-09/W/paper_ledger`
      -- the live ledger location is now empty for the next window.
3. Defense in depth: even though each analyzer call sees a single-run ledger,
   the analyzer's default latest-run scoping is verified from each
   analyzer.log "Scope:" line -- in-scope orders must equal total orders
   (N/N) and the run_id must be the window's own replay run.

Archive layout (ledger_archive/ is gitignored by design, like data/ and
analysis_out/ -- commits carry docs only; archives persist on disk):

    ledger_archive/overnight_sweep_2026-06-09/
        2026-05-30/{paper_ledger/, analysis_out/, replay.log, analyzer.log}
        2026-06-01/{paper_ledger/, analysis_out/, replay.log, analyzer.log}
        2026-06-09/{paper_ledger/, analysis_out/, replay.log, analyzer.log}

## 3. Per-window captures (Phase 2)

From each window's archived artifacts:
- emitted orders N + per-order `fill_adjusted_edge` (orders.jsonl; note this
  field is edge.py's walker #1 semantics on passers, per docs convention);
- the #2 CAPPED band and the #1 chase band (fill_adjusted_band.json +
  analyzer.log tables);
- rejection counts total and by reason (rejections.jsonl `reason` field,
  counted analysis-side);
- noarb_shadow record count + reasons breakdown, butterfly vs calendar
  (noarb_shadow.jsonl `reasons` list);
- regime label (vol_regime events in replay.log; rv_tracker reads sim-time
  post-C1).

## 4. Phase 3 fit-level cross-check plan

The ~25.7% calendar no-arb figure on 2026-06-01 is FIT-level (1319/5138
unique fits, docs/diag_noarb.md section 4), measured by a throwaway probe
(section 3; deleted). noarb_shadow.jsonl is EDGE-level (151 records on 0601),
so the banked sweep alone cannot reproduce a fit-level rate. To cross-check
the figure on 0530/0609 (and re-derive it on 0601), Phase 3 runs a scratch
probe driver `analysis_out/_sweep_noarb_fitrate.py` (gitignored, modeled on
the kept `analysis_out/_noarb_determinism_replay.py`): per window it
re-replays with a counting wrapper around `noarb_check_by_strike`
(pricing call counts, unique fits seen, unique fits violating, reasons),
writing to throwaway ledger dirs under analysis_out/. Read-only on
recordings, zero source changes, runs AFTER the banked sweep so Phase 2
artifacts are untouched.

## 5. Guardrails honored

- No source change to main.py / analyzer / pricing / filters; no new CLI
  flags; no new persistence. The sweep uses only: the existing replay CLI,
  the existing analyzer CLI flags (--out-dir), filesystem moves, and
  gitignored scratch analysis scripts.
- No floor change (MIN_EDGE=0.03 stays), no fill-model change, no clipping
  of #1 chase negatives (negatives are correctly-rejected losers).
- data/recordings is READ-ONLY throughout (replay cannot write it, sec. 1).
- The recorder / --record-feeds path is untouched (CPI capture 2026-06-11).
- HARD GATE result: isolation IS achievable with existing capabilities
  (sec. 2), so the sweep proceeds.
