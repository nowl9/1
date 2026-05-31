# Paper-Trading (Polymarket) + Deterministic Replay -- Resolved Design

Status: PLAN (capture-only). No implementation in this document. This file
records the RESOLVED design, the four resolved forks, the affirmed
persistence model, the build order, and the read-only spike finding that
gates Criterion 6.

Scope of this commit: documentation only. The single new file is this one.
Source is untouched. Probe scripts live under outputs/ and are not committed.

Author context: nested repo root C:\Users\mgill\Downloads\1-main\1-main
(the btc_pm_arb package). The sibling polykal-prediction-agent tree is out
of scope and untouched.

---

## 1. Replay readiness (SPIKE FINDING -- gates Criterion 6)

A read-only probe (outputs/probe_recordings.py) inspected every file under
data/recordings/. Findings below are the gate on whether a deterministic
replay run (Criterion 6) is feasible against the recordings we already have.

### 1.1 What the recordings are

Format: one gzipped JSONL file per (source, day, hour) at
data/recordings/{source}/{YYYY-MM-DD}/frames-HH.jsonl.gz. Per-line wire shape
(written by src/btc_pm_arb/feeds/recorder.py):

    {"ts": "<iso8601-recv-time>", "source": "deribit|kalshi|polymarket",
     "endpoint": "<rest-path or null>", "frame": "<raw frame as a string>"}

The outer "ts" is the agent's wall-clock receive time, stamped per frame for
ALL THREE sources. The inner "frame" is the raw venue payload (Deribit WS
message; Kalshi/Polymarket REST body) and carries its own venue timestamps
too (Deribit ticker data has a millisecond-epoch "timestamp"; Polymarket
/book has a millisecond-epoch "timestamp" string; Kalshi REST has no inner
per-frame timestamp but the outer recv "ts" covers it).

### 1.2 What the probe found (2026-05-30 capture)

    file                                       lines    window (UTC)              span
    deribit/2026-05-30/frames-18.jsonl.gz      18312    18:05:40 -> 18:07:09      ~89 s
    deribit/2026-05-30/frames-20.jsonl.gz      22703    20:35:36 -> 20:37:05      ~89 s
    kalshi/2026-05-30/frames-18.jsonl.gz         233    18:05:39 -> 18:07:06      ~87 s
    kalshi/2026-05-30/frames-20.jsonl.gz         233    20:35:36 -> 20:37:06      ~90 s
    polymarket/2026-05-30/frames-18.jsonl.gz     316    18:05:39 -> 18:07:09      ~90 s
    polymarket/2026-05-30/frames-20.jsonl.gz     312    20:35:36 -> 20:37:05      ~89 s

Polymarket frames carry "conditionId" in the gamma /markets envelope
(observed, e.g. 0x5929...0267). The /book frames are keyed by
asset_id / token_id, NOT by conditionId.

### 1.3 Replay-grade assessment

Per-tick timestamps on BOTH Deribit and Polymarket?
  YES. Outer recv "ts" on every line for all three sources, plus inner
  venue timestamps on Deribit and Polymarket. Adequate to drive a
  simulated clock from the recorded stream.

Format supports a replay reader injecting a SIMULATED clock?
  YES. The outer "ts" is a monotonic per-frame timeline a reader can use as
  the sim-clock source. One seam already exists: KalshiSettlementPoller
  takes an injectable `clock: Callable[[], datetime]`
  (paper_settlement.py:131) and tests already drive it deterministically.
  The freshness gates do NOT yet read an injected clock -- they call
  datetime.now(timezone.utc) directly (filters.py _reject_stale_data,
  _reject_expiry_bounds) and FeedHealthTracker.staleness_s(). Those couplings
  are exactly what Fork 3 must break (see section 4.3).

Contiguous, and enough duration to reach a settlement?
  NO. The capture is two DISJOINT ~90-second windows (18:05-18:07 and
  20:35-20:37) separated by a ~2.5-hour gap. Within a window the stream is
  contiguous; across the day it is not. ~90 seconds cannot organically
  advance a simulated clock to a contract expiry -- the Kalshi/PM BTC
  contracts in the capture expire days out (e.g. Kalshi close_time
  2026-06-01T03:59:59Z). A proportional replay of these files never reaches
  expiry, so settlement cannot be observed by replay alone.

### 1.4 Criterion 6 verdict: AT RISK (format green, duration red, mitigated)

  - Format readiness:   GREEN. Timestamps on all three sources; sim-clock is
                        injectable; a settlement clock seam already exists.
  - Duration / contiguity: RED for an organic-to-settlement replay. Two 90 s
                        windows with a 2.5 h gap; cannot reach expiry by
                        replaying ticks in proportion.
  - Mitigation:         Fork 2 (deterministic benchmark settlement) means
                        settlement does NOT require ticks AT the expiry
                        instant. If the replay reader supports a sim-clock
                        FAST-FORWARD / jump-to-expiry, the benchmark model
                        computes the settlement price deterministically from
                        the recorded surface, and Criterion 6 is achievable
                        on the CURRENT recordings.

Recapture recommended (NOT strictly blocking given the deterministic-
settlement design): a longer contiguous window that spans (or fast-forwards
cleanly to) at least one contract expiry, with per-case conditionId linkage
(see Carried Gap b, section 8). Until then, Criterion 6 must be implemented
with an explicit sim-clock jump-to-expiry, and the smoke test must assert
that jump is deterministic.

---

## 2. Acceptance criteria (the 6)

1. PM signals that survive all 12 gates un-short-circuited and routed to the
   shared FillSimulator. Today OrderManager.place() intercepts Polymarket
   signals as signal.polymarket_data_only and drops them (orders.py:285-289,
   352-358); PolymarketExecutor is a disabled stub. Crit 1 removes that drop
   and sends PM signals into the SAME FillSimulator that Kalshi uses.

2. A shadow no-op order intent is emitted BEFORE the sim evaluates the fill
   (records the intent the live path would have submitted, without submitting
   anything).

3. A fill produces a PaperLedger append (JSONL, run_id + mode stamped) and
   updates the PaperPositionTracker (positions derived from fill events).

4. At expiry, a DETERMINISTIC benchmark settlement (Fork 2) writes a
   PaperSettlementRecord.

5. tools/analyze_paper_ledger.py emits a joined.parquet row carrying
   slippage and P&L (fill-fidelity columns).

6. `--mode replay --duration N` reproduces all of the above from an EMPTY
   surface against the recordings, DETERMINISTICALLY, in CI.

Crit 1-2 are the real hidden scope (un-short-circuiting PM and the shadow
intent touch the live routing path). Crit 6 retires the seeded-cache debt:
once a cold replay from an empty surface works, tests no longer need a
hand-seeded pricing cache.

Note on "12 gates": filters.py currently defines 15 criterion functions
(_CRITERIA), several of which are no-op unless their context object is
present (vol_fit, correlated_exposure, feed_freshness, odds_velocity,
vol_regime). "12 gates" is the operative count of gates that bind on a PM
signal in paper mode; the un-short-circuit must not silently skip any of
them for PM.

---

## 3. Component design (section per component)

### 3.1 Mode switch + bounded-run flags + simulated-clock seam (build step 1)

Today main.py hardwires dry_run=True (main.py:1206) and exposes only
--record-feeds / --record-dir. No --mode, no --duration.

Add:
  - `--mode {live,replay}` (default live). Replay swaps the live feed tasks
    for a replay reader (build step 5) sourcing frames from --record-dir.
  - `--duration N` (seconds of SIMULATED time, or "to-expiry") to bound a
    run for CI.
  - A SimulatedClock object threaded through every freshness-sensitive call
    site. In live mode it delegates to datetime.now(timezone.utc); in replay
    mode it is advanced by the replay reader off the recorded "ts" stream.

The clock seam is a STEP-1 concern, not a step-5 afterthought (Fork 3). The
existing KalshiSettlementPoller.clock parameter is the precedent to follow;
the new work is to give filters.py and FeedHealthTracker the same injectable
clock instead of calling datetime.now() inline.

### 3.2 Un-short-circuit PM -> shared FillSimulator + book-walking (build step 2)

  - Remove the PM drop in OrderManager.place() for paper mode; route PM
    signals into the shared FillSimulator (the same instance Kalshi uses).
  - Extend FillSimulator from top-of-book full-or-nothing to BOOK-WALKING
    with PARTIAL fills (Fork 1). The snapshot already persists
    order_book_yes / order_book_no depth (fill_simulator.py BookSnapshot);
    today they are captured-but-not-consumed. Book-walking consumes them.
  - A signal that passed all 12 gates but faces an empty/thin book is an
    EXPLICIT, diagnosable outcome (a no_fill / partial with reason), not a
    silent optimistic full fill. This mirrors the empty-book gate already in
    filters.py (_reject_empty_book) and the diag that motivated it.

### 3.3 Deterministic benchmark settlement for PM (build step 3)

  - Today settlement is Kalshi-only and live (KalshiSettlementPoller polls
    GET /markets/{ticker} for status==settled + result in {yes,no}).
  - PM paper positions need a settlement path that is DETERMINISTIC so
    Criterion 6 can reproduce it. Decision: settle PM paper positions against
    a CF Benchmarks RTI-style benchmark MODEL evaluated at expiry (Fork 2),
    not against Polymarket's own oracle.
  - Known gap: PM resolves via its own oracle, not RTI. The benchmark is a
    deterministic MODEL of the settlement price -- a basis-like sim/real
    wedge. Accepted for v1; see Carried Gap a (section 8).
  - The settlement record shape (PaperSettlementRecord) and the
    position-close-on-settlement mechanics already exist for Kalshi and are
    reused; only the price source differs.

### 3.4 run_id / mode stamping + fill-fidelity columns (build step 4)

  - Every ledger append (order intent, fill, settlement, rejection) carries
    run_id and mode (live|replay) so runs are separable and joinable.
  - analyze_paper_ledger.py joins order -> fill -> settlement per
    client_order_id and emits joined.parquet with slippage (fill_price vs.
    intended limit / mid) and realized P&L columns.
  - run_id + the parquet join is the run-comparison primitive (replay run A
    vs. replay run B vs. a live run).

### 3.5 Replay reader + cold-path smoke (build step 5 -- highest risk)

  - A reader that opens the gzipped JSONL recordings, replays frames through
    the SAME normalizer the live feeds use, advances the SimulatedClock off
    the recorded "ts", and supports a jump-to-expiry fast-forward so
    settlement is reachable despite the ~90 s window (see section 1.4).
  - Cold-path smoke: start from an EMPTY pricing surface (no seeded cache),
    replay, and assert the full chain (signal -> shadow intent -> fill ->
    ledger -> settlement -> parquet) reproduces deterministically.
  - Highest-risk step; de-risked by the section 1 spike (format is replay-
    grade; duration is handled by the jump-to-expiry design).

### 3.6 Shadow no-op order intent (build step 6)

  - Before the FillSimulator evaluates, emit the order intent the live path
    would have submitted (venue, side, limit, size, snapshot) as a no-op
    ledger record. Submits nothing. This is the audit trail that the live
    routing path WOULD have produced, captured without execution.

---

## 4. Resolved forks

Each fork: Options / Decision / Reasoning / Flip-condition.

### 4.1 Fork 1 -- Thin-book fill model

Options:
  (a) Full fill at ask (current FillSimulator behavior: top-of-book,
      full-or-nothing).
  (b) Book-walking with PARTIAL fills against captured depth.

Decision: (b) book-walking + partial fills in v1.

Reasoning: this is a scalping strategy; fill realism IS the strategy
validation, not a later refinement. Full-fill-at-ask is the empty-book
optimistic defect simply relocated from discovery into execution -- it would
make every passing signal look fillable at the quoted ask. An empty or thin
book must surface as an explicit, diagnosable decision (the signal passed 12
gates and faced an empty book, and the simulator said no_fill / partial with
a reason), not as a silent optimistic fill.

Flip-condition: revert to full-fill-at-ask ONLY if v1 is explicitly declared
plumbing-only (wiring the pipeline end-to-end with fill realism deferred). It
is not.

### 4.2 Fork 2 -- Settlement source

Options:
  (a) Poll Polymarket's own resolution oracle (live, non-deterministic
      w.r.t. a replay).
  (b) Deterministic benchmark MODEL (CF Benchmarks RTI-style) evaluated at
      expiry.

Decision: (b) benchmark-based deterministic settlement.

Reasoning: determinism is what makes Criterion 6 possible -- a replay must
reproduce the settlement price from the recorded surface alone, with no live
oracle call. A benchmark model evaluated at the expiry instant does that.

Flip-condition / known gap: PM actually resolves via its own oracle, not RTI.
The benchmark is a deterministic MODEL of settlement, introducing a basis-
like sim/real wedge (the paper settlement can differ from the real oracle
outcome). Accepted as a known v1 gap (Carried Gap a). Flip toward a recorded-
oracle-outcome settlement if/when oracle resolutions are captured in the
recordings and the wedge proves material in P&L analysis.

### 4.3 Fork 3 -- Replay pacing and the clock

Options:
  (a) Real-time-paced replay (sleep to match recorded inter-frame gaps).
  (b) As-fast-as-possible replay driven by recorded timestamps, with an
      INJECTED simulated clock that the freshness gates read.

Decision: (b) as-fast-as-possible with an injected simulated clock.

Reasoning: REQUIRED, not optional. If replay runs as-fast-as-possible while
the freshness gates read wall-clock (datetime.now()), then within
milliseconds of wall-clock the recorded ticks look minutes/hours stale and
the freshness/expiry gates reject everything -- the pipeline produces zero
signals. The gates must read SIM-time, advanced off the recorded "ts"
stream, so a tick recorded at 18:05:40 is "fresh" when the sim-clock is at
18:05:41 regardless of wall-clock. This is why the clock seam is a build
step 1 concern (section 3.1), not a step 5 concern.

Flip-condition: adopt real-time pacing only if a future need for wall-clock-
faithful latency reproduction appears (e.g. modeling signal-to-fill latency
in wall-clock terms); not anticipated for v1.

### 4.4 Fork 4 -- Execution abstraction

Options:
  (a) Per-venue executor adapters (one protocol implementation per venue).
  (b) A single PaperExecutor with per-venue DIALECT config.

Decision: (b) single PaperExecutor + dialect config.

Reasoning: Kalshi execution is idle and Polymarket is the one live paper
venue, so a per-venue protocol abstraction has no SECOND concrete instance to
justify it -- it would be speculative generality. The FillSimulator is
already venue-agnostic (it takes side / limit_price / size / snapshot, not a
venue). A dialect config captures the per-venue book-shape differences
(Kalshi: bid levels per side with asks derived from the complement;
Polymarket: separate bid/ask books) without a second protocol.

Flip-condition: promote to per-venue adapters WHEN the one-touch / range
pricer lands AND Kalshi rejoins execution with divergent microstructure that
the dialect config can no longer cleanly express.

---

## 5. Persistence (affirmed)

Model: JSONL append-only ledger (os.fsync per append; see paper_ledger.py)
plus in-memory positions DERIVED from the fill event stream
(PaperPositionTracker).

  - Crash recovery: re-read the JSONL and rebuild positions from events.
  - Reproducibility: the event stream is deterministic, so a replay produces
    an identical ledger.
  - Run comparison: run_id stamping + a parquet join across runs.

Sufficient at current low volumes. No database in v1. (Revisit only if write
volume or query patterns outgrow append-and-rescan -- not anticipated.)

---

## 6. Build order

1. Mode switch + bounded-run flags, INCLUDING the simulated-clock seam
   (--mode, --duration, SimulatedClock threaded into filters.py and
   FeedHealthTracker). Clock seam designed here, not deferred.
2. Un-short-circuit PM -> shared FillSimulator + book-walking / partial
   fills.
3. PM deterministic benchmark settlement.
4. run_id / mode stamping + fill-fidelity (slippage, P&L) columns in the
   ledger and analyze_paper_ledger.py.
5. Replay reader + cold-path smoke from an empty surface (HIGHEST RISK;
   de-risked by the section 1 spike).
6. Shadow no-op order intent before the sim.

---

## 7. Out of scope

  - One-touch / range (barrier and band) product pricer. These are tracked-
    but-never-signaled today (filters.py _reject_one_touch, _reject_range)
    and belong to a SEPARATE ultraplan. Do NOT preclude barrier support
    later; also do NOT assume it.
  - Live-trading credentials (POLYMARKET_PRIVATE_KEY / MetaMask). Paper mode
    only; no real order submission.

---

## 8. Carried gaps

(a) Settlement wedge (from Fork 2). The deterministic benchmark model is not
    Polymarket's real oracle; paper settlements can differ from real
    resolutions by a basis-like amount. Known, accepted for v1; quantify in
    P&L analysis and flip Fork 2 if material.

(b) Recordings lack per-case conditionId linkage (from the PM-classifier
    work). Observed nuance: conditionId IS present in the Polymarket gamma
    /markets envelope, but the /book frames are keyed by asset_id / token_id,
    and the question-text-keyed denylist is not conditionId-keyed. This
    affects replay fidelity (joining a book frame back to its classified
    case) and the robustness of the denylist. Recapture with explicit per-
    case conditionId linkage on the NEXT recording. Until then the replay
    reader must join book frames to cases by asset_id / token_id via the
    gamma envelope.
