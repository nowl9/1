# Arb Recording Campaign -- Operator Runbook

_Purpose: settle the edge-vs-threshold question for the options->PM digital arb
strategy. Does honest fill-adjusted edge clear a tradeable threshold, and how
often, across market regimes? One quiet capture (2026-05-30) showed ~1.7% max,
below the 3% prod floor. That is one sample in one regime. This campaign turns
"one number" into "a distribution across regimes."_

_This is an operator checklist, not code. Runs on your Windows box against the
deterministic replay pipeline already built. Repo root: the NESTED
C:\Users\mgill\Downloads\1-main\1-main._

_Last updated: 2026-06-11. Canonical home is now docs/arb_campaign_runbook.md
in the nested repo (version-controlled); the loose copy in Downloads\clawd mds
is a mirror as of this date and can be deleted at leisure._

---

## Campaign stage status (2026-06-11)

| Stage | What | Status |
|---|---|---|
| 0 | Recorder hardening | CLOSED -- CPI failure root-caused (ENOSPC -> silent _disable; disk starved by ambient use, not the recorder). Hardened: preflight >=20GB, watchdog, ENOSPC stop-loud, --duration fixed, clean Ctrl-C, keep-awake. Commits 36d649d..d8264a6 + ef71c6e. |
| 1 | Live drill | CLOSED 2026-06-11 -- real outage detected at 122s on all six streams, full recovery, clean shutdown, 6/6 trailers. Instrument TRUSTED. |
| 2 | FOMC Jun 16-17 capture | NEXT -- see "Stage 2: FOMC plan" below. |
| 3 | Post-FOMC replay + Q1 | PENDING -- Q1 answerable on BOTH venues post-FOMC (Kalshi depth fix is retroactive), subject to the terminal-Kalshi open question. |

Both recovery paths have now been observed live: watchdog restart for task
death, and feed self-reconnect for network loss. Operator note: deribit
recovery lags by its backoff position (<=60s) post-outage -- do not panic if
deribit trails the other five streams back.

---

## What this campaign can and cannot tell you (read first)

CAN: whether *standing* edge (a mispricing that sits in the book long enough to
snapshot) exists on terminal BTC binaries, at what magnitude, and how often,
across regimes.

CANNOT: anything about *latency* edge. The replay pipeline snapshots once per
scan; a transient spot-vs-stale-odds gap opens and closes between snapshots.
A thin result here is a verdict on the OPTIONS-ARB thesis only -- NOT evidence
that these markets lack edge. Do not let a thin arb campaign talk you out of
the latency thesis; they measure different things in the same order book.
(Double-latency is currently DEMOTED to a Data Streams concern -- see todo.md.)

The widened recorder banks spot / chainlink / pm5min streams in every window,
so each capture you take here is ALSO latency raw material for later.

---

## The regimes to capture

Record one (or more) window in EACH. The point is variance across regimes -- a
quiet baseline plus the conditions where mispricing is most likely to widen.

| # | Regime | Why it matters | When to catch it |
|---|---|---|---|
| 1 | Quiet baseline | The control. Re-confirms the ~1.7% floor in calm conditions. | Low-vol overnight / weekend lull |
| 2 | High realized vol | Mispricings widen when the underlying moves fast and books lag. | Large intraday BTC move (>2-3%) |
| 3 | Macro print | Scheduled vol spike; biggest divergences cluster here. | CPI, FOMC, NFP release windows |
| 4 | High volume / busy hours | Tightest books -- likely the LOWEST edge. Tests the efficient case. | US market open overlap |
| 5 | Near-expiry roll | As terminal contracts approach expiry, pricing dynamics shift. | Day-of / hours-before a daily/weekly close |

You do not need all five before learning something -- 1, 2, and 3 are the
highest-information set. 4 is the "is it ever efficient" check. 5 is optional.

NFP and CPI windows were missed (recorder was not yet trustworthy); both are
mooted by FOMC as the regime-3 source.

---

## Stage 2: FOMC plan (Jun 16-17)

- **Monday:** 1h dress rehearsal -- full capture start/stop cycle, confirm
  trailers and stream tags.
- **Tuesday night:** start the real capture and leave it running.
- **Wednesday ~14:00 ET (print):** confirm all six streams alive at the print.
- **Label the regime by hand** in the results table -- the recorder does not
  tag regime, you do.

Expectation calibration (from the pmxt probe's FOMC screening,
outputs/pmxt_probe_2026-06-11.md): the regime is surprise-dependent. The
2026-03-18 print repriced near-money Polymarket strikes 1.5-3.6c, peaking by
+5m and reconverging over 19-35m; the 2026-05-06 print was muted (~1c, ~2.5h
reconvergence). Opportunity window = minutes, and the 5s scan cadence sits
comfortably inside it. A muted print is information, not failure. (Caveat
carried from the probe: candles are depth-blind and cannot answer Q1 --
screening evidence, not edge evidence.)

Disk budget: 74.4GB free as of 2026-06-11; FOMC 48h burn estimate ~17GB ->
~57GB floor. The preflight enforces >=20GB regardless, so a starved disk now
fails loud at startup instead of dying silent mid-window.

Kalshi leg expectations for Wednesday: the depth fix is live, but if the OPEN
QUESTION below resolves to barrier-only, the Kalshi leg yields one-touch
barrier rejections regardless of the fix, and Q1 fill-walk evidence rides on
Polymarket's recorded books. Either outcome is a result; record it.

---

## Recording -- how

Run the agent in live mode with capture on. Capture is public-data only; no
trading, no credentials beyond the public feeds.

    py -3.12 src/btc_pm_arb/main.py --record-feeds

The recorder is hardened as of Stage 0 (config knobs: recorder_*):
- Preflight: refuses to start under 20GB free (shutil.disk_usage).
- Watchdog: 120s stream-silence restart / 10s scan / 5GB disk soft-alarm.
- ENOSPC: stops LOUD with trailers banked (no more silent _disable).
- --duration works; clean Ctrl-C shutdown; keep-awake holds the box up.

Guidance:
- DURATION: longer and contiguous beats short and fragmented. The original
  capture is two disjoint ~90s windows -- too short to span a settlement and
  too sparse to be statistically meaningful. Aim for 15-30 min contiguous per
  regime window, more for macro prints (start ~10 min before the print, run
  through ~20 min after; for FOMC, the Stage 2 plan above runs much longer).
- TIMING: for regimes 2 and 3, the value is in the MINUTES AROUND the move.
  Start recording BEFORE the event, not after the gap has already closed.
- LABEL: each run writes data/recordings/{source}/{date}/frames-HH.jsonl.gz.
  Note which date/hour maps to which regime in the results table below -- the
  recorder does not tag regime, you do.
- The widened streams (spot/chainlink/pm5min) record automatically under the
  same flag. Confirm they appear. If chainlink self-disables (prod allowlist
  blocks the Polygon RPC host polygon-bor-rpc.publicnode.com), the other
  streams keep going -- fine for the arb campaign, note it for latency.
- OUTAGES: the instrument is drill-proven -- watchdog restarts dead tasks,
  feeds self-reconnect after network loss. deribit rejoins last (backoff
  position, <=60s). Trailers tell you afterward what every stream saw.

Standing rule: data/recordings/ is READ-ONLY once written, and the
recorder/capture code path is FROZEN until the campaign's capture windows are
done.

---

## Replaying + measuring -- per window

For each recorded window, replay through the deterministic pipeline and read
the fill-adjusted edge. The pipeline is already built and proven deterministic
(re-proven 2026-06-11: the 0530 re-replay through the fixed Kalshi depth
parser was deterministic with zero verdict changes, as pre-registered).

    py -3.12 src/btc_pm_arb/main.py --mode replay --replay-date {YYYY-MM-DD}

Then read the joined ledger:

    py -3.12 tools/analyze_paper_ledger.py   (-> analysis_out/joined.parquet)

What to read off each replay:
1. SIGNAL COUNT at the prod floor (MIN_EDGE=0.03) vs the 1% code-default
   floor. Zero at 3% / some at 1% repeats the 2026-05-30 pattern; signals at
   3% is the result you are hunting.
2. EDGE DISTRIBUTION, not just the max. Record the top few conservative edges
   and roughly how many cleared each threshold (1% / 2% / 3%).
3. FILL-ADJUSTED vs THEORETICAL edge. This is the column that answers read B
   (is thin edge real after fills). A 1.7% theoretical edge that survives to
   ~1.5% fill-adjusted is very different from one that collapses to 0.3% --
   the slippage column tells you which.
4. SETTLEMENT outcome (win/loss) if the window jump-to-expiry settles -- small
   sample, but accumulates.
5. KALSHI MATCHES: the depth fix (6c2d5af) is RETROACTIVE (frames carry raw
   orderbook_fp bodies), so the first window with terminal Kalshi matches
   makes the Kalshi fill numbers real. Watch whether Kalshi matches are
   terminal or 100% one_touch_barrier rejections (0530 was the latter) -- that
   feeds the open question below.

IMPORTANT: run the 1% floor with the PRODUCTION .env UNTOUCHED (the 1% is a
FilterConfig data-collection floor, an override for measurement -- NOT a
loosening of the prod gate). The whole point is to measure what edge EXISTS,
not to lower the bar you would trade at. Never loosen the prod floor; the old
"recalibrate MIN_EDGE" idea is superseded by this guardrail.

---

## Open question (answer inside the Stage 3 goal, not before)

Does ANY in-mandate (>=1 DTE) terminal Kalshi binary exist? Evidence pointing
to no: Tier-1 series are all min/max (barrier) class; 0530's 53 Kalshi matches
were 100% one_touch_barrier rejections; May monthlies were path-pinned at 0.00
through the FOMC statement. If confirmed barrier-only: (a) Wednesday's Kalshi
leg yields barrier rejections regardless of the depth fix; (b) Q1 fill-walk
evidence rides on Polymarket's recorded books; (c) the barrier/range scorecard
row promotes to likely-the-only-executable-form-on-Kalshi -- live execution of
terminal-binary edge currently has no Kalshi home.

---

## Results table (fill this in as you go)

This is the deliverable. It turns scattered run logs into a verdict.

| Date/hr | Regime | Signals @3% | Signals @1% | Max cons. edge | Median of those | Fill-adj holds? | Settled W/L | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-05-30 | quiet (baseline) | 0 | 2 | 1.7% | ~1.x% | TBD | 1W/1L | the existing capture; re-replayed 2026-06-11 through the fixed Kalshi depth parser -- deterministic, zero verdict changes |
|  | high-vol |  |  |  |  |  |  |  |
| 2026-06-17 (planned) | macro print (FOMC) |  |  |  |  |  |  | Stage 2; label by hand |
|  | busy hours |  |  |  |  |  |  |  |
|  | near-expiry |  |  |  |  |  |  |  |

---

## How to read the finished table (the decision)

- SIGNALS APPEAR AT 3% in high-vol / macro regimes -> read B partly answered:
  the strategy has tradeable standing edge, it is just regime-gated. Next
  question becomes position sizing and frequency, not "does edge exist."
- STAYS THIN (max ~1-2%) ACROSS ALL REGIMES, fill-adjusted -> strong evidence
  the standing-edge options-arb thesis does not clear a tradeable bar on
  terminal binaries. That is a real (if disappointing) answer, and it shifts
  weight toward (a) the excluded barrier/range products, where a one-touch
  pricer would let you measure a different, likely-lazier-priced universe
  (NOTE: pending the open question above, this may be the ONLY executable form
  on Kalshi), or (b) the latency thesis, which this campaign cannot see but
  whose raw data you have now been banking in every window.
- FILL-ADJUSTED EDGE COLLAPSES vs theoretical -> the edge is a fill artifact;
  the book-walking simulator is doing its job by exposing it. This is the
  empty-book / optimistic-fill failure mode caught honestly.

A thin result is NOT a failed campaign. It is the market answering the
strategy's core question, cheaply, before you committed capital or more build
effort to it.

---

## Two data-fidelity notes (from the recorder-widening sample)

- pm5min polls /book at 1s. That proves a lag EXISTS but may be too coarse to
  SIZE a sub-second lag. Relevant to latency analysis later, not to the arb
  campaign -- but if you record for both, know the 1s ceiling.
- chainlink captures every poll; the latency signal is the round-NUMBER
  transitions (fresh pushes), not poll count. In a quiet window you may capture
  zero actual round changes. Again a latency-analysis note, not an arb one.
