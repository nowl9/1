# TODO -- BTC Prediction-Market Arb (campaign state + open items)

_Last updated: 2026-06-11. Canonical home is now docs/todo.md in the nested repo
(version-controlled); the loose copy in Downloads\clawd mds is a mirror as of this
date and can be deleted at leisure. Previous update was 2026-05-31._

---

## WHERE WE ARE (read this first)

The arb recording campaign is mid-flight. Stages 0 and 1 are CLOSED; the
instrument (recorder) is hardened and TRUSTED. Stage 2 -- the FOMC Jun 16-17
capture -- is NEXT. Stage 3 (post-FOMC replay + analysis) answers Q1: does
honest fill-adjusted edge clear a tradeable threshold, and how often, across
regimes?

Strategy map, current: the options->PM digital arb edge (SVI surface vs
terminal binaries) is the active thesis under measurement. The double-latency
thesis is DEMOTED to a Data Streams concern -- its raw material (spot /
chainlink / pm5min) is banked automatically in every capture window, but no
build effort is committed. polykal directional remains a separate repo, out of
scope.

---

## STAGE STATUS

- **Stage 0 CLOSED** -- recorder hardening. The CPI capture failure was
  root-caused: ENOSPC -> silent _disable; disk was starved by ambient machine
  use, not by the recorder. Recorder hardened: preflight >=20GB via
  shutil.disk_usage, watchdog (120s silence / 10s scan / 5GB disk soft-alarm),
  ENOSPC now stops LOUD with trailers banked, --duration fixed, clean Ctrl-C,
  keep-awake. Commits 36d649d..d8264a6 + flake fix ef71c6e.
- **Stage 1 CLOSED 2026-06-11** -- live drill. A real outage was detected at
  122s on all six streams with full recovery; both recovery paths now observed
  (watchdog restart for task death, feed self-reconnect for network loss);
  clean shutdown; 6/6 trailers. Instrument TRUSTED. Note: deribit recovery
  lags by its backoff position (<=60s) post-outage.
- **Stage 2 NEXT** -- FOMC Jun 16-17 capture. Monday 1h dress rehearsal;
  capture starts Tuesday night; streams confirmed alive at the Wed 14:00 ET
  print; regime labeled by hand. Expectation calibration from the probe
  screening: regime is surprise-dependent -- a muted print is information, not
  failure. Operational detail lives in docs/arb_campaign_runbook.md.
- **Stage 3 (post-FOMC)** -- replay + Q1. With the Kalshi depth fix being
  retroactive, the first window with terminal Kalshi matches makes Kalshi fill
  numbers real -- Q1 becomes answerable on BOTH venues, subject to the OPEN
  QUESTION below.

---

## LANDED SINCE LAST UPDATE (2026-05-31 -> 2026-06-11)

- **Kalshi depth FIXED** (6c2d5af / 8d5d5a7 / 8905a5e). RETROACTIVE: frames
  carry raw orderbook_fp bodies, so the fix applies to already-recorded data.
  _derive_ask_depth; 13 regression tests pinned on the real 0611 frame; the
  0530 re-replay through the fixed parser was deterministic with zero verdict
  changes, as pre-registered.
- **Dashboard/RiskManager goal COMPLETE 2026-06-11** (commits 9b2cae2 C1
  dashboard truth / 853b564 C2 retirement, risk.py deleted whole / bb87609 C3
  payload pins; suite 889 -> 880 = -12 convicted tests +3 pins; commits
  verified present in git log). The dead RiskManager is retired; the dashboard
  risk card is now READ-ONLY and shows the ENFORCING RiskLimits caps
  500/5000/500 plus the current-run block-event count from the funnel counter
  (it previously displayed the dead 1000/10000 -- wrong on BOTH caps). The
  dead-ended POST /api/risk-config + RiskConfigRequest + sliders were deleted
  (convicted: the write was clobbered by the next snapshot push and never read
  back). The exposure gauge was REPOINTED (D3): numerator
  paper_performance.total_exposure_usd (already published), denominator
  max_global_exposure -- the gauge is truthful for the first time. Orphaned
  config fields + .env.example entries removed. Scope was D1-MINIMAL by
  explicit decision. Live smoke passed: enforcing payload served live, POST
  probe 405, six streams trailer-verified, clean Ctrl-C through the
  main.py:2009 finally-block adjacency.
- **Disk CLOSED.** The 98->75 drift was one-time (cleanup-day churn); 74.4GB
  free on 2026-06-11 (73.35 at the Goal 1 smoke preflight); FOMC 48h burn
  ~17GB -> ~57GB floor. The preflight enforces 20GB regardless.

---

## OPEN ITEMS

- **POST-FOMC FOLLOW-UP (new):** full legacy PositionTracker +
  SettlementMonitor retirement. Constructed-but-empty remnants were left
  deliberately (conviction document: Goal 1 Phase 1 gate report, D1-minimal
  deferred scope). Includes: SettlementMonitor polling an empty dict,
  always-empty dashboard payload fields s.positions / s.settlements plus their
  frontend consumers, ~15-20 test_integration deletions.
- **MICRO-ITEM (next src-touching goal, opportunistic):** stale comment at
  edge.py:388 references the deleted risk.size_for_signal; user .env still
  carries inert MAX_POSITION_USD / MAX_TOTAL_EXPOSURE_USD keys (delete at
  leisure; extra="ignore" makes them harmless).
- **DATED FOLLOW-UP ~2026-07-06:** re-probe KXBTCMAX100 (its Oct-01 close
  enters the 90d window). Manual todo, deliberately NOT scheduled as an
  autonomous task.
- **OPEN QUESTION (answer inside the Stage 3 goal, not before):** does ANY
  in-mandate (>=1 DTE) terminal Kalshi binary exist? Evidence pointing to no:
  Tier-1 series are all min/max (barrier) class; 0530's 53 Kalshi matches were
  100% one_touch_barrier rejections; May monthlies were path-pinned at 0.00
  through the FOMC statement. If confirmed barrier-only: (a) Wednesday's
  Kalshi leg yields barrier rejections regardless of the depth fix; (b) Q1
  fill-walk evidence rides on Polymarket's recorded books; (c) the
  barrier/range scorecard row PROMOTES to
  likely-the-only-executable-form-on-Kalshi -- live execution of
  terminal-binary edge currently has no Kalshi home.

---

## LOW-VOL TRACK (scoped 2026-06-11 -- thesis documented; no build committed)

_The scorecard must not be all high-vol-dependent; the market is quiet most
of the time. If Stage 4's Q1 read (the post-FOMC decision branch) stays
negative, this track BECOMES the campaign; if positive, it diversifies it.
Either branch needs it scoped. Maker-side feasibility evidence from the
banked quiet windows: docs/diag_lowvol_maker_2026-06-11.md._

- **LEAD: options-informed passive quoting (maker) on Kalshi.** EVIDENCE
  CHAIN: the quiet windows measured a STRUCTURAL 16-18pp taker haircut --
  the [1,3%) band collapsed to -13.5..-15.5% fill-adjusted across
  0530/0601/0609, caused by the book-walker reaching past empty near-mid
  into a 0.99 wall. That haircut is the resting maker's revenue, measured
  from the taker's side. Corollary: the "redundant" quiet tape is this
  strategy's PRIMARY dataset (its home regime). Risk profile: adverse
  selection + inventory -- a NEW scorecard row, not a tweak to the taker
  thesis. First build item IF activated: emit resting-bid depth fields
  (deliberately not emitted today -- see the _derive_ask_depth docstring in
  feeds/kalshi.py). Open question: Kalshi fee treatment for makers (no
  rebates known).
- **SECOND: barrier/range universe.** Measured excluded flow --
  one_touch_barrier rejections appear in EVERY banked window (97/302/45 on
  0530/0601/0609); possibly the ONLY executable Kalshi form per the
  terminal-Kalshi open question above; needs genuine barrier pricing (big
  lift).
- **POST-ALWAYS-ON: breaking-news vol.** Ex-post rv_1h labeling of the
  rolling tape; unscheduled vol may test the arb thesis BETTER than
  scheduled prints (differential venue reaction speed, vs MMs
  pre-positioned at known print times).

---

## VENUE / TOOLING SCORECARD

- **IBKR -- desk research CLOSED 2026-06-11.** No >=1 DTE BTC binaries on
  ForecastEx (econ/climate only) or CME (daily-expiry only); the platform's
  only qualifying BTC contracts are Kalshi-routed. Residuals: Kalshi-routed
  commission vs direct 0.07*C*P*(1-P) (one number, post-edge-proof relevance);
  3.12% APY position carry (matters at size); CME 0DTE universe (different
  strategy, parked). No /goal warranted.
- **PMXT -- RECLASSIFIED** from delete-candidate to research-tier tool,
  quarantined from the live path (Node sidecar = shared SPOF; its abstraction
  hides the raw bodies that made the depth fix retroactive).
  feeds/discovery.py stays dead-as-wired.
- **PMXT probe COMPLETE 2026-06-11** (outputs/pmxt_probe_2026-06-11.md): no
  new executable universe -- Polymarket is the only sized, two-sided >=1 DTE
  BTC venue reachable via pmxt (already covered by our recorder); Gemini Titan
  indicative/zero-size; crypto-native venues thin or AMM; the Kalshi adapter
  is stale-host + credential-gated (quarantine vindicated). Router/clustering
  is PAID hosted-only -- polykal-via-pmxt = paid dependency (note on the
  polykal row). 3yr OHLCV = screening only; epistemic line verbatim: "Candles
  are depth-blind and cannot answer Q1. Screening evidence, not edge
  evidence." Tier-2 re-probe absorbed: all Tier-2 still zero contracts,
  KXBTCW gone from catalog, the C4 don't-poll decision stands; no FOMC-week
  discovery gap (18 qualifying contracts, all Tier-1). FOMC screening result:
  the 2026-03-18 print repriced near-money Polymarket strikes 1.5-3.6c,
  peaking by +5m, reconverging 19-35m; 2026-05-06 was muted ~1c with ~2.5h
  reconvergence. Opportunity window = minutes (5s scan cadence comfortably
  inside).
- **Maker-quoting row:** resting-bid depth fields are deliberately not emitted
  (see the _derive_ask_depth docstring) -- first build item if this row is
  ever activated. Now the LEAD candidate of the LOW-VOL TRACK above.
- **Barrier/range row:** see the OPEN QUESTION above -- pending Stage 3, this
  row may PROMOTE to likely-the-only-executable-form-on-Kalshi.

---

## STANDING GUARDRAILS (carry into every goal)

- Never loosen MIN_EDGE or any floor / threshold / gate / fill model. The old
  "MIN_EDGE recalibration" idea is superseded by this guardrail: measurement
  uses the FilterConfig data-collection floor, never a loosened prod gate.
- Never clip or "rescue" fill-sim #1 chase-adjusted negatives; they are
  correctly-rejected losers, not missed signals.
- data/recordings/ is READ-ONLY. The recorder/capture path is FROZEN
  mid-campaign.
- Goals run sequentially; commit-gated builds; ASCII-only; commits via
  commit_msg.txt + git commit -F. (Baked into .claude/commands/goal.md.)

---

## CORRECTION LOG (kept so it isn't re-introduced)

- 2026-05-31: the hypothesis "the btc_pm_arb gates (data freshness, odds
  velocity, vol regime) are fingerprints of the latency strategy; the project
  was originally built for it" was inferred from gate *names* before reading
  source. It does NOT survive reading the code: polykal contains no such
  gates, and the btc_pm_arb freshness/expiry/edge gates serve the options-arb
  thesis. Treat any latency strategy as fully greenfield.

---

## CLOSED

- Wi-Fi hardening.
- Handoff queue #5 (the overnight sweep covers both bands, all three windows).
- @1% measurement pass (subsumed by the sweep band characterization; reopen
  only if Stage 3 needs order-emission economics at a lower floor).
- MIN_EDGE recalibration (superseded by the never-loosen guardrail).
- Double-latency (demoted to Data Streams; raw material banked per window via
  the spot/chainlink/pm5min aux streams; no build commitment).
- PM NO-side parity (ticks carry both sides; PM is data-only).
- IBKR venue expansion (closed -- see scorecard).
- PMXT new-venue expansion (closed -- see the probe row in the scorecard).
- Dashboard cap mismatch (CLOSED by 9b2cae2..bb87609).
- Shadow mode / dashboard sliders (deleted by Goal 1) / VenueAdapter / SQLite /
  PMXT-as-live-integration / latency-model: never-shipped design claims,
  accepted as closed.
- NFP + CPI captures (missed, mooted by FOMC).
