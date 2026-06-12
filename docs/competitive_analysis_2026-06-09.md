# Competitive Analysis: Signal-Gen + Execution Architecture

Date: 2026-06-09
Scope: signal generation and execution architecture only (data ingestion and
frontends excluded). Compared against our 4-layer design: Data -> Pricing ->
Signal -> Execution.

Framing note: most public BTC prediction-market bots run a **cross-venue YES+NO
under-$1.00 arbitrage** edge (buy both sides for < $1, settle for $1). That is a
*different edge source* from ours (options-implied probability oracle, single PM
leg) and one we have already and correctly declined to adopt. So this analysis
mines **architecture**, not strategy. Several of these repos explicitly disclaim
production-readiness; none is endorsed as profitable. We are reading structure.

Repos examined:
- TopTrenDev/polymarket-kalshi-arbitrage-bot (Rust, cross-venue PM arb)
- ImMike/polymarket-arbitrage (Python, FastAPI, cross/single-venue PM arb)
- XanderRobbins/Arbitrage-Free-Volatility-Surface (Python, options pricing engine)
- polykal-prediction-agent (ours, directional; combine-path reference)

---

## Comparison Table

| Repo / Pattern | What they do better than us | What we do better (or differently) | Actionable pattern to adopt (or skip) |
|---|---|---|---|
| **TopTrenDev** (Rust PM arb) | Execution concerns are split into named single-responsibility modules: `event_matcher`, `arbitrage_detector`, `trade_executor`, `position_tracker`, `settlement_checker`, `monitor_logger`, plus `config` with a `dry_run` flag and a dual-strategy orchestrator. Settlement and matching each get their own seam. Per-slot log files. | We have a real fair-value model (SVI/digital oracle), not a book inequality. We have a queue-aware FillSimulator (FIFO + book-walking) and event-sourced JSONL portfolio replay; they detect inequality and fire with no fill-realism layer visible. Our matching lives in the signal layer with polarity/product-type handling they don't need. | **ADOPT** the module-per-concern decomposition for our Execution layer: promote settlement detection to its own `settlement_checker` seam and matching to its own `matcher` seam (both have been repeated bug sources for us). **SKIP** their `arbitrage_detector` edge logic (different strategy). |
| **ImMike** (Python PM arb) | First-class, declarative **risk-limit layer**: `max_position_per_market`, `max_global_exposure`, `max_daily_loss` as config gates distinct from the edge gate (`min_edge`). Orthogonal mode switches: `trading_mode` (dry_run) x `data_mode` (real/simulation). Broad universe (watches 10k+ markets). | Our signal gating is far richer (DTE, freshness, odds velocity, vol regime, fill-adjusted edge, confidence) vs their single `min_edge`. Critically, their edge gate appears **theoretical-only** - the exact trap that collapsed our quiet-regime signal once real book depth was walked. We have SimulatedClock + deterministic replay and run_id/mode stamping. | **ADOPT** the risk-limit layer as config-declared caps evaluated *separately* from edge gates - we gate on edge but our exposure/loss caps are not first-class. Clean additive port. **ADOPT** the orthogonal mode axes (action x data-source) - we half-have this (PAPER/LIVE/REPLAY + shadow no-op); formalize the decoupling. |
| **XanderRobbins** (options pricing engine) | Chainable pipeline making each pricing stage explicit and individually testable: `load_data -> compute_ivs -> check_arbitrage -> fit_svi -> calibrate_heston`. An **explicit static no-arb check stage** (put-call parity, butterfly, calendar) gates *before* the fit. Robust IV root-finding (Newton-Raphson + Brent fallback). | Ours is wired into a live agent: Levy TWAP basis adjustment, digital pricing via call-spread with bid/ask bounds, feeding a signal layer. Theirs is a standalone library (no PM leg, no execution, no perp/crypto basis). | **ADOPT** the explicit no-arb check as a **fail-closed gate inside our pricing engine**: validate butterfly/calendar no-arb on the SVI surface at the digital's strikes before it is allowed to emit a probability. A bad surface should emit zero signal, not phantom edge. Directly hardens our top fear (artifact edge). |
| **polykal** (ours, combine reference) | Has the `Signal` + `SignalAggregator` seam (`signals/base.py`, `strategy/aggregator.py`): a typed `Signal` (estimated_prob, confidence, direction, signal_type) fused by confidence-weighted averaging - the seam btc_pm_arb structurally lacks (we have one signal source). Has a learning loop (`signal_tracker` -> `resolution_harvester` -> `weight_calibrator`) and clean 1/4-Kelly sizing. | Our feeds (WebSocket, depth, fast path) beat polykal's REST polling outright. Our pricing engine, fill simulator, and deterministic replay are far ahead. Our edge is price-neutral; polykal's is directional. | **ADOPT** the `Signal` seam **as scaffold only** (single signal flows through, no aggregation active) so future edge sources plug in without a refactor - gated behind measure-before-build. The learning loop ports onto our parquet ledger later; 1/4-Kelly ports into the risk layer later. **SKIP** the four directional signals and `polykal/markets/` connectors (strictly inferior). |

---

## Top 3 Patterns as Scoped /goal Prompts

All three are additive, fail-closed, and respect the standing invariants: never
loosen the edge floor, never clip the #1 chase-adjusted negatives, never touch
the prod live `.env`, no premature combine, no `/goal` against data that does
not exist. Each is written to run as one `ultracode` session with an in-prompt
STOP gate between diagnose and implement.

### /goal #1 - No-arb surface gate (from XanderRobbins). Highest priority.

```
/goal Add a fail-closed no-arbitrage validation gate to the SVI pricing engine,
so a malformed surface cannot emit digital probabilities that become phantom
signals.

Run with /effort ultracode. Phases are gated: do not start a phase until the
prior is committed. Run from nested root C:\Users\mgill\Downloads\1-main\1-main.

CONTEXT
- Pricing engine fits a Gatheral SVI surface, then prices binary digitals via
  call-spread replication with bid/ask bounds (src/btc_pm_arb/pricing/).
- Today the surface can fit and emit a digital prob even when the smile violates
  static no-arb (butterfly/calendar), which can manufacture artifact edge.

PHASE 1 - DIAGNOSE (commit: probe + findings file only)
- Locate where the SVI surface emits the digital probability consumed by the
  signal layer. Write the exact module:function path to docs/diag_noarb.md.
- Throwaway probe (PowerShell here-string): fit the current surface on one
  banked window and check (a) butterfly no-arb (call convexity in strike /
  non-negative risk-neutral density) and (b) calendar no-arb (total implied
  variance non-decreasing in T). Record pass/fail counts to the findings file.
  Delete probe after.
- HARD GATE: if no banked window violates no-arb, STOP and report - do not build
  a gate against a failure mode that does not occur. Note in findings and END.

PHASE 2 - IMPLEMENT (only if Phase 1 found real violations)
- Add pure noarb_check(surface) -> (ok: bool, reasons: list[str]) covering
  butterfly + calendar at the strikes/expiries used for the digital.
- Wire it as a gate BEFORE the digital prob reaches the signal layer. On
  failure: emit no signal (fail-closed), log a noarb_reject record to the
  existing JSONL. Do NOT raise; do NOT fall back to a best-effort surface.
- Additive only. Do not touch FilterConfig.min_conservative_edge or any edge
  floor. Do not alter fill-sim #1 chase-adjusted negatives.

PHASE 3 - VERIFY
- Tests: a clean surface passes; a hand-built butterfly-violating smile and a
  calendar-violating pair each fail-closed with zero signal.
- py -3.12 -m pytest fully green. Prove additive scope via git numstat.
  Multi-paragraph commit via commit_msg.txt. ASCII only.

OUT OF SCOPE: changing the SVI fit method; dynamic/drift arbitrage; any
execution-layer change; any edge-threshold change.

SUCCESS CRITERIA: a surface failing butterfly or calendar no-arb at the
digital's strikes emits zero signal and one JSONL noarb_reject record; all prior
tests pass; numstat shows additions plus minimal wiring only.
```

### /goal #2 - First-class risk-limit layer (from ImMike).

```
/goal Introduce a first-class risk-limit layer with config-declared caps
evaluated separately from edge gates, so position/exposure/loss limits are
enforced independently of signal quality.

Run with /effort ultracode. Phases gated; commit each before the next. Run from
nested root C:\Users\mgill\Downloads\1-main\1-main.

CONTEXT
- Signal gates today (DTE, freshness, odds velocity, vol regime, fill-adjusted
  edge, confidence) decide IF a contract is mispriced. There is no separate
  layer deciding whether we are ALLOWED to add given current exposure.
- Reference (ImMike/polymarket-arbitrage): max_position_per_market,
  max_global_exposure, max_daily_loss as declarative config, distinct from edge.

PHASE 1 - DIAGNOSE (commit: findings file only)
- Trace the order-intent path from signal acceptance to PaperLedger/OrderManager.
  Write the exact insertion point (post-signal, pre-order) to
  docs/diag_risklimits.md.
- Confirm where current exposure and realized daily P&L are reconstructable from
  the event-sourced JSONL replay. Record the field names.
- HARD GATE: if exposure/daily-P&L cannot be read at order time without new
  persistence, STOP and report the gap before building.

PHASE 2 - IMPLEMENT
- Add RiskLimits (pydantic-settings; OS env wins over .env) with
  max_position_per_market, max_global_exposure, max_daily_loss; sane defaults;
  PAPER values only - do NOT touch prod live .env.
- Add pure check_risk(intent, portfolio_state) -> (allow: bool, reason),
  evaluated AFTER edge gates, BEFORE order submission. On block: no order, log
  risk_block + reason to JSONL.
- Evaluate caps against run_id-scoped event-sourced state (use existing run_id
  stamping so cross-run accumulation cannot contaminate the cap - prior misread).

PHASE 3 - VERIFY
- Tests: a signal passing all edge gates but breaching each cap is blocked with
  the right reason; within-cap signals pass; cross-run records do not inflate the
  cap.
- py -3.12 -m pytest green. numstat additive. commit_msg.txt. ASCII only.

OUT OF SCOPE: position sizing math (Kelly - separate goal); any edge-gate change;
live .env; changing how P&L is computed.

SUCCESS CRITERIA: each of the three caps, when breached, blocks the order and
writes one run_id-scoped risk_block JSONL record with reason; edge gating
unchanged; all prior tests pass; numstat additive.
```

### /goal #3 - Signal seam scaffold (from polykal). Lays combine seam; combines nothing.

```
/goal Introduce the Signal seam as a single-signal passthrough so the current
options-implied probability flows through a typed Signal abstraction WITHOUT
activating any aggregation. This lays the combine seam cheaply; it does NOT
combine anything.

Run with /effort ultracode. Phases gated; commit each before the next. Run from
nested root C:\Users\mgill\Downloads\1-main\1-main.

CONTEXT
- btc_pm_arb has exactly one signal source today (options-implied prob from the
  SVI/digital engine). polykal has a Signal dataclass (estimated_prob,
  confidence, direction, signal_type) + a SignalAggregator. We want the SEAM only.
- Combine is PARKED until >=1 strategy shows fill-surviving edge. This goal adds
  ZERO aggregation logic and ZERO second signal.

PHASE 1 - DIAGNOSE (commit: findings file only)
- Locate the single point where the options-implied probability + confidence are
  produced and handed to the matching/edge step. Write the exact module:function
  to docs/diag_signalseam.md.
- Record the current fields carried (prob, confidence, direction, freshness, etc.)
  so the dataclass is a faithful superset.

PHASE 2 - IMPLEMENT
- Add a frozen Signal dataclass (estimated_prob, confidence, direction,
  signal_type='options_implied', plus existing freshness/meta fields).
- Have the existing producer return Signal; the edge step consume Signal. Pure
  passthrough: decisions must be unchanged.
- Do NOT add SignalAggregator. Do NOT add a second signal_type. Do NOT add
  weighting. A one-line TODO comment may mark where an aggregator would later
  attach - no code.

PHASE 3 - VERIFY
- Replay one banked window before and after; the set of emitted signals AND their
  fill-adjusted values must be identical (regression test asserting equality).
- py -3.12 -m pytest green. numstat near-additive. commit_msg.txt. ASCII only.

OUT OF SCOPE: SignalAggregator; any second signal source; weight calibration;
learning loop; Kelly sizing; any edge-floor change; clipping #1 negatives.

SUCCESS CRITERIA: the options-implied signal now flows as a typed Signal; a
before/after replay of the same window produces identical emitted signals and
identical fill-adjusted edges; no aggregation code exists; all prior tests pass.
```

---

## /goal + ultracode Usage Review vs Handoff Discipline

### The one friction point worth fixing

Our current diagnose-then-fix loop spends **two full human-relay round-trips per
fix**: (1) MG runs a diagnostic `/goal`, pastes findings into the architecture
chat; (2) I scope a fix `/goal` from those findings; MG runs it. That split
exists for a pre-ultracode reason - a single Opus Cowork context could not be
trusted to diagnose honestly *and then* fix without a human gate confirming the
diagnosis first. The friction *is* the mandatory human diagnosis-confirmation
gate. It is a latency tax on every fix.

`ultracode` is purpose-built to remove exactly this. Its understand -> change ->
verify orchestration runs the "understand" workflow in a separate clean-context
Claude *before* the "change" workflow - which is the diagnose-before-fix gate,
but in-harness. So the fix is to **collapse the two round-trips into one
`/goal`** and convert the human gate from a mandatory step into an exception
handler that only fires on a surprising diagnosis.

### Tightened prompt template (this is the body for .claude/commands/goal.md)

```
/goal <one-sentence outcome>

Run with /effort ultracode. Phases are gated: do not begin a phase until the
prior is committed. Run from nested root C:\Users\mgill\Downloads\1-main\1-main.

PHASE 1 - DIAGNOSE (commit: probe + findings file only)
- <what to locate / measure>; write exact module:function paths + evidence to
  docs/diag_<name>.md.
- Reproduce the failure mode on banked data; record the observation.
- HARD GATE: if the diagnosis contradicts the hypothesis OR the failure mode does
  not reproduce, STOP, write why to the findings file, and END without
  implementing. Do not fix a problem you did not confirm.

PHASE 2 - IMPLEMENT (only if the gate passed)
- <additive change>. Fail-closed on ambiguity.
- INVARIANTS (never violate): do not loosen FilterConfig.min_conservative_edge or
  any edge floor; do not clip or hide fill-sim #1 chase-adjusted negatives; do
  not touch prod live .env; additive only unless this is an explicit extraction.

PHASE 3 - VERIFY
- <specific tests proving the success criteria>.
- py -3.12 -m pytest fully green. Prove scope with git numstat. Multi-paragraph
  commit via commit_msg.txt. ASCII only.

PHASE 4 - HANDOFF DELTA
- Append a dated entry to handoff_YYYY-MM-DD.md: what changed, new test count,
  any new diagnostic artifact. (Long autonomous sessions should write their own
  handoff delta, not leave it for the operator.)

OUT OF SCOPE: <explicit list>.
SUCCESS CRITERIA: <verifiable, observable end state>.
```

What changes vs today: the **HARD GATE in Phase 1 replaces the human-relay
diagnosis-confirmation round-trip**. The diagnose-first discipline is preserved -
it becomes an in-prompt STOP condition rather than a copy-paste back to the
chat - so MG only re-enters when the gate fires (a surprising or unconfirmed
diagnosis). That turns the per-fix human tax into an exception handler. Phase 4
moves the handoff write into the session, matching longer autonomous runs.

What does NOT change: every load-bearing invariant stays explicit in the prompt,
because the Fable 5 system card's documented failure mode is precisely the
"subtly wrong while running unattended" case (e.g. counting one error type while
another accumulates). The harness gets the relay; the prompt keeps the guardrails.
```

---

# VERIFICATION ANNEX (2026-06-11, master 094df6a)

Appended by the D2 provenance-gap-closure goal. Everything above this
section is the original loose file from
"C:\Users\mgill\Downloads\clawd mds\prediction markets\", copied
byte-preserved (SHA256 of the original content, prior to this append:
22324FF4EEF1BF384EB9576948DFE08716D7478E13E1420A4B9A704C22A8941B).
The 06-10 handoff summarized this doc as "3 patterns, all resolved".
The enumeration below verifies every pattern/claim the doc actually
makes about this repo against master HEAD 094df6a (880 tests green) --
seven items, not three.

VERDICT: all enumerated patterns RESOLVED on master. No findings.

1. No-arb surface gate (XanderRobbins row ADOPT + scoped /goal #1)
   - Claimed: adopt a fail-closed butterfly/calendar no-arb gate
     inside the pricing engine.
   - Verified: RESOLVED -- implemented, as a deliberate SHADOW variant
     rather than fail-closed: src/btc_pm_arb/pricing/noarb.py:1
     (butterfly Durrleman g(k) + calendar total-variance checks at the
     digital's own strikes), wired flag-at-pricing at
     src/btc_pm_arb/main.py:454-463 and emitted at the edge step as
     noarb_shadow JSONL records. Commits 7e2b894 (feat), c80bafa
     (Phase 3: 19 tests, byte-identical replay). Fail-closed
     suppression is deliberately deferred, gated on the quiet-vs-CPI
     shadow comparison (noarb.py:30-35); calendar-violation rate
     characterized at 25.7% across 3 windows (6f5f829).

2. First-class risk-limit layer (ImMike row ADOPT + scoped /goal #2)
   - Claimed: adopt max_position_per_market / max_global_exposure /
     max_daily_loss as declarative caps evaluated separately from the
     edge gates.
   - Verified: RESOLVED -- src/btc_pm_arb/execution/risk_limits.py:119,
     :121, :126 (the exact three caps), enforcement at
     risk_limits.py:176-189, evaluated after edge gates and before
     placement. Commits 808019e (Phase 1 diag, hard gate passed),
     6a838bf (feat), 4f13849 (21 tests incl. run_id scoping).
     Follow-ons: 9b2cae2 (dashboard mirrors the ENFORCING caps),
     853b564 (dead legacy RiskManager retired).

3. Orthogonal mode axes (ImMike row ADOPT, below the doc's top-3 cut)
   - Claimed: formalize the decoupling of action mode x data-source
     mode (the doc's own grading at write time: "we half-have this").
   - Verified: RESOLVED (present on master) -- the two axes are
     decoupled, each with a single source of truth: data axis =
     clock.mode (live|replay), src/btc_pm_arb/main.py:220-226 ("--mode
     without a second source of truth"), stamped with run_id on every
     ledger append (main.py:250-252;
     src/btc_pm_arb/execution/paper_ledger.py:148-152); action axis =
     OrderManager(dry_run, dry_run_paper_mode), main.py:216. No
     additional formal config object exists; the decoupling the row
     asked for is structural.

4. Signal seam scaffold (polykal row ADOPT + scoped /goal #3)
   - Claimed: introduce a typed Signal dataclass as a single-signal
     passthrough (combine seam only, no aggregation).
   - Verified: RESOLVED by diagnosis -- the scoped /goal's own hard
     gate fired and the passthrough was correctly NOT implemented
     (commit 7108aeb; verdict at docs/diag_signalseam.md:11). The
     chain is already typed end-to-end (DigitalPrice frozen dataclass,
     src/btc_pm_arb/pricing/digital_pricer.py:76, through CacheEntry,
     MatchResult, EdgeResult, ArbitrageSignal -- diag doc section 2),
     and a Signal.estimated_prob scalar would be a lossy narrowing
     because the edge step consumes the full bid/mid/ask triple
     (src/btc_pm_arb/signals/edge.py:125-151). The typed chain remains
     unflattened on master.

5. Module-per-concern execution decomposition (TopTrenDev row ADOPT,
   below the doc's top-3 cut)
   - Claimed: promote settlement detection and matching to their own
     single-responsibility seams.
   - Verified: RESOLVED (present on master) -- settlement detection
     lives in dedicated modules
     (src/btc_pm_arb/execution/settlement.py:1 SettlementMonitor, plus
     paper_settlement.py and benchmark_settlement.py); matching lives
     in its own module, src/btc_pm_arb/signals/matcher.py. Note: the
     legacy SettlementMonitor + PositionTracker pair is slated for
     post-FOMC retirement in favor of the paper-path modules, which
     preserves (not removes) the seam.

6. SKIP recommendations (TopTrenDev arbitrage_detector edge logic;
   polykal directional signals + markets connectors)
   - Claimed: do not adopt.
   - Verified: RESOLVED -- absent from src/: no cross-venue YES+NO
     detector and no directional-signal ports (grep for
     arbitrage_detector / cross-venue logic returns no matches); the
     single signal source remains the options-implied probability.

7. Tightened /goal prompt template ("this is the body for
   .claude/commands/goal.md")
   - Claimed: collapse the diagnose+fix human round-trips into one
     /goal with an in-prompt HARD GATE replacing the mandatory human
     diagnosis-confirmation step.
   - Verified: RESOLVED -- .claude/commands/goal.md shipped (commit
     af91336) carrying the template's load-bearing content in adapted
     form: diagnose-first + HARD GATE with stop-on-pass literalism
     (goal.md:12-19), commit-gated build steps with pytest green,
     numstat proof and commit_msg.txt (goal.md:20-22), standing
     invariants/out-of-scope (goal.md:35-43), env/ASCII conventions
     (goal.md:8-10). The gate has fired in practice on item 4 above --
     the exception-handler design works as intended.

Cross-check vs the 06-10 handoff's "3 patterns, all resolved": the
three scoped /goal prompts (items 1, 2, 4) are confirmed resolved, and
the doc in fact carries four additional verifiable claims (items 3, 5,
6, 7), all also resolved. No unresolved pattern; no finding.
