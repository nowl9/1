# Diagnosis: risk-limit layer (Phase 1 findings)

Date: 2026-06-09.  Base: master @ cb2c05e, 801 tests green (verified:
`py -3.12 -m pytest -q` -> `801 passed` on this tree).

Goal under diagnosis: config-declared caps (max_position_per_market,
max_global_exposure, max_daily_loss) evaluated AFTER the edge/confidence
gates and BEFORE order placement, against run_id-scoped event-sourced
state.  PAPER values only.

Method note: every file:line claim below was independently adversarially
verified by 5 parallel read-only review agents instructed to refute;
none refuted, corrections were folded in.

## HARD GATE VERDICT: PASS

All three cap inputs (per-market position, global exposure, realized
daily P&L) are reconstructable at order time from the existing
event-sourced JSONL streams, run_id-scoped, WITHOUT new persistence.
The run_id stamp already exists on every persisted record; the gap is
purely read-side (no production code filters by run_id today).
Phase 2 may proceed.

One scope boundary confirmed: only the REALIZED variant of daily P&L is
reconstructable.  Marks are never persisted (PaperPosition.current_mid
is set from live ticks in mark_to_market, paper_positions.py:264-277,
and written nowhere), so a daily cap that included unrealized losses
WOULD require new persistence.  The goal specifies realized daily P&L,
so this is in-bounds, but max_daily_loss must be documented as
realized-only.

## 1. Insertion point

`btc_pm_arb.main : Agent.run_scan_pipeline : main.py:611`

Insert the risk check inside the `if self.dry_run:` branch
(main.py:610), immediately BEFORE
`order = await self.order_mgr.place(sig, size_usd=_BASE_SIZE_USD)`
(main.py:611-613).

Why exactly there:

- It is strictly AFTER every edge/confidence gate.  The gate chain is
  `self.signal_filter.filter(...)` (main.py:482-490; DTE, freshness,
  odds velocity, vol regime, fill-adjusted edge -- all 12 criteria) and
  the confidence backfill (main.py:499-504).  Verified: lines 588-609
  contain only the `signal.observed` log and comments -- there is no
  further gate between the loop head (`for sig in passing:`,
  main.py:588) and the place() call.  Any confidence-reading risk
  check is only valid after the 499-504 backfill, which this point
  satisfies.
- It is strictly BEFORE the order is built.  The Order object is
  constructed inside `OrderManager.place` (orders.py:451-459); a block
  at the insertion point touches NO OrderManager state: no fingerprint
  registration (orders.py:420-424), no `_orders` registry entry
  (orders.py:460), no executor submit (orders.py:473), no
  `paper_orders_placed` funnel increment (main.py:615).
- Blocking AFTER place() is wrong for a sharper reason than state
  noise: place() registers the signal fingerprint at orders.py:424
  before returning, and orders.py:421-423 then returns None for that
  fingerprint forever.  A post-place block would therefore permanently
  suppress the signal even after exposure later drops below the cap --
  a one-shot no-retry suppression.  Pre-place blocking leaves the
  fingerprint unregistered, so the intent is re-evaluated on the next
  scan and trades normally once headroom returns.
- Single chokepoint, both modes: `run_scan_pipeline` has exactly two
  production call sites -- the live scan task (main.py:1396, def
  `_scan_task` main.py:1373) and the replay reader
  (feeds/replay.py:374).  main.py:611 is the only production
  `order_mgr.place` call site, main.py:633 -> 765 -> 782 the only
  `append_order` path, main.py:821 the only new-fill
  `paper_positions.record_fill` site outside startup rehydration
  (main.py:297-306).  One insertion covers live and replay.

Known consequence (accepted, to be handled in Phase 2): because the
dedupe fingerprint is only registered inside place(), a blocked signal
that keeps passing the edge gates re-arrives every ~5 s scan tick
(_SCAN_INTERVAL_SECS, main.py:105).  Without its own dedupe the block
path would write one risk_block record per tick for a persistent
breach.  Phase 2 must decide: either accept per-tick records (honest
"breach ongoing" telemetry) or dedupe per (fingerprint, reason) --
the Phase 3 test contract is "exactly one risk_block per blocked
intent", which the per-tick form satisfies (each tick is one intent).

The intent fields needed by the check are all available pre-place:
contract key = (sig.pm_quote.source, sig.pm_quote.contract_id), side
derivation `"yes" if sig.trade_side == "buy_yes" else "no"` (identical
to orders.py:427), size = `_BASE_SIZE_USD` = 200.0 (main.py:107).

## 2. No existing permission layer (confirmed)

`execution/risk.py` contains a RiskManager with per-contract/total
exposure checks, but it is dead on the order path: constructed at
main.py:197-200, its `check()` (risk.py:166) and `size_for_signal()`
(risk.py:192) have ZERO production call sites -- the only `self.risk`
reads are the dashboard config snapshot (main.py:1137-1143).  Its
input type, the legacy `PositionTracker` (main.py:196), is never
operatively fed fills on the paper path: the only feed site,
`agent.tracker.record_fill(o)` in `_order_refresh_task`
(main.py:1427), iterates `filled_orders()` (orders.py:490-491) and no
Order ever reaches FILLED in paper mode (OrderManager is built with
`dry_run_paper_mode=dry_run` at main.py:205, making both executors'
refresh() no-ops, orders.py:219-225 and 346-350; `main()` hardcodes
`dry_run=True` at main.py:1713).  So today NOTHING evaluates exposure
permission before a paper order -- the goal's premise holds.

## 3. The three cap inputs: fields, accessors, run_id scoping

### Event streams (ground truth, all run_id-stamped on disk)

All five record models in `execution/paper_ledger.py` carry
`run_id: str = ""` and `mode: str = "live"`.  `PaperLedger._append`
stamps both at append time (paper_ledger.py:499) via
`record.model_copy(update=...)` and fsyncs per append
(paper_ledger.py:505-509), so same-process reads see every
current-run record.

CRITICAL ASYMMETRY (the trap behind the prior 137-record misread):

- The stamp is applied to the SERIALIZED COPY only.  The caller's
  in-memory record object keeps `run_id=""` (model_copy does not
  mutate the original).  In-process records passed to
  `paper_positions.record_fill` (main.py:821) are therefore
  UNSTAMPED in memory; records rehydrated from disk
  (main.py:297-306) carry their REAL stamped run_id.  A run-scoped
  view must NOT filter in-process events by the record's run_id
  field (always "") -- it must trust call-site provenance for
  current-run events and filter by `record.run_id == self.run_id`
  ONLY when replaying from disk.
- The replay readers (paper_ledger.py:513-526) do NOT filter by
  run_id, and startup rehydration (main.py:297-306) feeds ALL
  records into `self.paper_positions` regardless of run_id.  The
  in-memory tracker is therefore CROSS-RUN CONTAMINATED: positions
  key by (platform, contract_id, side) (paper_positions.py:136), so
  fills from different runs on the same triple compound irreversibly
  into one weighted-average entry (paper_positions.py:202-214).
  Live and replay also share the same ledger dir
  (settings.paper_ledger_dir, main.py:240-242; replay never
  overrides it).  `self.paper_positions` MUST NOT be used directly
  for cap evaluation.
- `PaperPosition` has no run_id field (paper_positions.py:55-87);
  run identity is unrecoverable from tracker state alone.
- Pre-stamp legacy records ("None-tagged") deserialize to
  `run_id=""`.  The scoping filter `record.run_id == self.run_id`
  (current run_id is a uuid4 hex, never "") excludes both legacy and
  other-run records in one predicate.

### (a) Per-market position

Source events: orders.jsonl (PaperOrderRecord: client_order_id,
platform, contract_id, side, run_id) joined to fills.jsonl
(PaperFillRecord: client_order_id, fill_price, fill_size_usd,
fill_outcome, run_id) on client_order_id.  Accumulation rule mirrors
`PaperPositionTracker.record_fill` (paper_positions.py:158-215):
skip no_fill / invalid fills (paper_positions.py:173-183), sum
`fill_size_usd` per (platform, contract_id, side).  Run-scoped
position for the intent's market = that sum over current-run events
for the intent's (platform, contract_id) -- per-market cap should
aggregate across sides or per (market, side); decide in Phase 2
config docs (reference repo caps per market).

### (b) Global open exposure

Same accumulation as (a) over ALL current-run triples, minus settled
ones: a triple is closed when a settlements.jsonl record matches it --
`PaperPositionTracker.settle` keys by (platform, contract_id, side)
(paper_positions.py:217-240).  Equivalent in-memory accessor shape:
`total_exposure_usd()` = sum of filled_size_usd over open positions
(paper_positions.py:326-327) -- but computed on a RUN-SCOPED tracker
instance, not `self.paper_positions`.

### (c) Realized daily P&L

Source events: settlements.jsonl (PaperSettlementRecord:
realized_pnl, settled_at, run_id; appended run_id-stamped at
paper_settlement.py:380-381 (Kalshi poller) and
benchmark_settlement.py:163-164 (PM benchmark settler), both
append-before-settle).  Daily realized P&L = sum of `realized_pnl`
over current-run settlements whose `settled_at` falls on the current
UTC day.  Day boundary MUST come from the sim-clock seam
(`self.clock.now()`, clock.py:71-86) so replay buckets by sim-time;
`settled_at` is sim-clock-correct in both settlers.  (Note:
PaperFillRecord.filled_at is wall-clock even in replay, main.py:818
-- irrelevant here since no cap input uses filled_at, but do not
day-bucket fills.)  Settlements are stamped with the run that WROTE
them, so a prior-run position settling during the current run counts
toward the current run's daily loss -- correct semantics for a
daily-loss brake.

### Recommended Phase 2 shape (read-side only, no new persistence)

Maintain a second, RUN-SCOPED `PaperPositionTracker` instance plus a
small list of (settled_at, realized_pnl) settlement events:

- at startup, rehydrate it from the same inline replay loop
  (main.py:297-306) but ONLY for records with
  `record.run_id == self.run_id` (covers same-run restart /
  deterministic replay re-run);
- update it at the same call sites that update the main tracker
  (main.py:821 record_fill; settlement append sites via the existing
  poller/settler tracker hooks or by reading back) -- provenance, not
  the in-memory run_id field, establishes "current run";
- evaluate `check_risk(intent, portfolio_state)` against that view.

This is derived-view computation in the existing pattern (the tracker
is already "a derived view ... reconstructed on agent restart",
paper_positions.py:1-8).  No schema change, no new files, no new
fields: run_id stamping (the only persistence prerequisite) already
exists.

### Config seam

`config.py` Settings is pydantic-settings `BaseSettings` with
`env_file=".env"` (config.py:7-13); OS env wins over .env by
pydantic-settings source priority -- the repo convention the goal
requires.  RiskLimits can follow the same pattern as its own
BaseSettings class without touching the existing Settings fields or
any .env file.  NOTE: `settings.max_position_usd` /
`max_total_exposure_usd` (config.py:72-73) feed only the dead
RiskManager snapshot -- Phase 2 must add NEW fields, not repurpose
these, to keep the dashboard snapshot semantics unchanged.

## 4. Out-of-scope confirmations

- FilterConfig.min_conservative_edge (main.py:190-194,
  settings.min_edge=0.01) untouched by this design -- the risk layer
  sits after the filter, reads none of its config.
- The #1 chase-adjusted negatives (rejection path,
  main.py:705-763) are upstream of the insertion point on the
  REJECTED branch; the risk layer only sees PASSING signals.  No
  interaction.
- P&L computation unchanged: the risk layer only READS
  realized_pnl from existing settlement records.
- Recorder/--record-feeds path: not touched by any file this goal
  will modify (main.py scan path, new risk module, config addition,
  tests).
