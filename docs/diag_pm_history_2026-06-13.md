# PM HISTORY -- high-vol episode census + settlement calibration (2026-06-13)

> Candles and settlements: depth-blind, gross-of-fee, queue-blind. P1 sizes
> the opportunity REGIME, P2 attributes the premium at the MID level. Neither
> answers fill survival (Q1).

Sandboxed desk study on 3 years of Polymarket BTC binary history (span
2023-06 .. 2026-06-12). Zero repo/src changes; data/recordings untouched;
throwaway venv `outputs/pmxt_probe/venv` (pmxt 2.49.8 + Node sidecar) reused.
All scripts and raw artifacts live under gitignored `outputs/pm_history/`.

## TL;DR

- **P1 (census)**: organic high-vol windows are not scarce -- they are the
  norm. Over the last 12 full months, counting 30-minute wall-clock buckets
  in which at least one near-money contract with >=7 days to expiry moved
  >=2.5c within 15 minutes: **~274 organic buckets/month (~137 h) vs ~9
  scheduled-macro buckets/month -- a ~30:1 ratio**. At >=1 DTE the tape
  saturates (~75% of all clock time at 2.5c). If a scheduled print is muted,
  the organic month replaces it ~30x over. An always-on recorder is
  capacity-bound, not event-bound.
- **P2 (calibration)**: pre-registered verdict = **MARKET-WRONG at the mid
  level, but only mid-life**. Contracts priced 0.60-0.80 settle YES ~2.3-3.1pp
  below their price pooled (Wilson CI excludes price), ~-4.8pp at DTE 4-14
  and ~-10.6pp at DTE 15-42, while DTE 1-3 is *calibrated* (+2.4pp, sign
  flipped) and the final-day grid shows +2.9pp. Direction and size match the
  bias-footprint probe's +4.5pp YES premium vs our SVI fair
  (docs/diag_bias_footprint_2026-06-11.md): the premium is real at the mid
  level and decays to zero into expiry. Fade-the-YES (buy NO) mid-life is
  consistent with settlement reality -- at the MID level only; fills (Q1)
  remain unanswered.

## 0. Method, provenance, honesty notes

- **Discovery degraded honestly off pmxt**: pmxt 2.49.8's closed-market
  search is structurally broken at this scale -- `fetchMarkets(status=closed)`
  fans out 1000+ parallel Gamma page requests inside the sidecar
  (`paginateSearchParallel`, `Promise.all (index 1001)`) and kills its own
  throttler ("Throttler queue full (max depth 1000)"), even fresh after a
  sidecar restart. Discovery therefore used Polymarket's public Gamma REST
  API directly (tag `bitcoin` id=235, closed markets, end-date-windowed
  offset pagination with adaptive window splitting under the ~10k offset cap).
  **Candles stayed on pmxt** `fetch_ohlcv` as tasked: 8,666 chunked calls
  (5m fidelity, 14d max per call -- empirically: 1m <= ~7d, 5m <= ~14d,
  1h <= ~7d, 1d unusable) + ~180 1m refinement calls, **zero fetch errors**.
- Retention checked: 1m/5m candles exist for markets settled as far back as
  Jan-2024; no retention cliff inside the window that matters.
- **Adversarial script audit before trusting results** (12 subagents): 4
  serious claims -> 3 real and fixed before the production runs (nearest-
  candle snap bias; float-floor bucket misassignment at 0.95; an 'etf'
  blacklist regex eating 5 legitimate price strikes), 1 refuted with data
  (Gamma `outcomePrices` IS index-aligned with `outcomes`; the challenger
  mistook the ~70% NO-resolution base rate for an ordering artifact --
  verified on four known-outcome markets, e.g. "Will BTC hit $44,000 in
  2023?" -> ["1","0"]).
- Macro calendar: 96 events (36 CPI, 36 NFP, 24 FOMC statements) 2023-06 ..
  2026-06-12, from the Fed's calendar page + BLS schedules; 2025 shutdown
  reschedules verified by press coverage (Sep-2025 CPI on 2025-10-24;
  Oct-2025 CPI canceled; Nov-2025 CPI on 2025-12-18; Sep-2025 NFP on
  2025-11-20; Oct+Nov NFP combined on 2025-12-16). CPI/NFP 08:30 ET, FOMC
  14:00 ET, DST-correct UTC conversion.
- **Side correction for the record**: the 2026-06-11 pmxt probe report
  analyzed a "2026-05-06 FOMC statement". There was no May-2026 FOMC
  meeting: the actual spring statement was **2026-04-29** (Fed press release
  and minutes pages confirm; next is 2026-06-17). That probe's "muted print"
  read for 2026-05-06 was an analysis of a non-event day; its 2026-03-18
  print stands.

## 1. Universe (inventory)

120,526 closed Bitcoin-tagged Polymarket markets enumerated for 2023-06 ..
2026-06-12 (by end date). Families by question shape (planner
classification):

| family             | n       | note                                        |
|--------------------|---------|---------------------------------------------|
| updown             | 83,397  | 15m/1h/daily up-or-down recurrents -- excluded (not threshold) |
| other_btc          | 23,972  | no price strike in question (incl. "higher at 3pm than 2pm" hourlies) -- excluded |
| terminal           | 6,343   | "above/below/between $X on <date>" -- census+calib |
| terminal_intraday  | 4,011   | "greater/less than $X on <date> at 5PM ET" dailies -- census+calib |
| one_touch          | 2,643   | "reach/hit/dip $X in/by <period>" -- census+calib |
| other (price-adjacent/non-BTC) | ~160 | excluded |

Analysis set after floors (life >= 18h, volume >= $5k, Yes/No binary,
parseable dates): **8,095 markets** (terminal 5,727 / one_touch 1,772 /
terminal_intraday 596; 8,091 UMA-resolved). Median life: terminal 7.0d,
one_touch 1.7d, terminal_intraday 1.3d.

HONESTY ON "3 YEARS": Polymarket BTC *price-threshold* coverage effectively
begins 2023-12 (single markets), is thin through 2024-08 (1-10
contracts/month), and only becomes a dense multi-ladder catalog from
2024-09. The census below is a ~21-month effective census with a thin head,
not a uniform 36-month one. Near-money coverage (>=1 contract in [0.10,
0.90]) is continuous from 2024-03 onward (every 30m bucket covered most
months).

## 2. P1 -- displacement-episode census

Pre-registered definitions (fixed before results): near-money = 5m close in
[0.10, 0.90] at baseline; displacement = |close(t) - close(t-15m)| >= theta,
theta in {1.5c, 2.5c, 3.5c}; metric = distinct 30-minute wall-clock buckets
containing >=1 trigger (monotone in theta by construction); scheduled =
bucket start within [release-30m, release+3h] of CPI/NFP/FOMC; DTE band =
time from trigger to contract close anchor.

A first-cut "merge episodes with gap<=30m into windows" metric was DISCARDED
as misleading: at 1.5c the modern tape chains into mega-windows (163,991
contract episodes -> 2,891 windows whose count *rises* with theta). Bucket
counts replaced it; the discarded numbers remain in the artifact JSON.

### 2.1 The base-rate answer (last 12 full months, 2025-05 .. 2026-04)

| theta | DTE>=1: organic buckets/mo (range) | sched/mo | DTE>=7: organic buckets/mo (range) | sched/mo |
|-------|------------------------------------|----------|------------------------------------|----------|
| 1.5c  | 1,382 (1,237-1,461) = ~93% of clock | 20.3    | 648 (505-777)                       | 15.0     |
| 2.5c  | 1,128 (683-1,340)                   | 18.6    | **274 (213-361)**                   | 9.2      |
| 3.5c  | 844 (374-1,088)                     | 16.3    | 132 (78-163)                        | 5.5      |

(1,488 thirty-minute buckets exist in a 31-day month. Scheduled share of
all triggered buckets at 2.5c across the whole span: CPI 180 / NFP 164 /
FOMC 128 buckets.)

**Deliverable answer**: at production-like horizons (DTE>=7), a muted
scheduled print forfeits ~9 buckets/month of scheduled displacement at 2.5c;
the same month supplies ~274 organic buckets (~9/day). Organic:scheduled ~=
30:1 (24:1 at 3.5c). At DTE>=1 the question inverts: 75-93% of all clock
time has SOME displaced near-money contract -- harvesting is bounded by
recorder capacity and contract selection, not by event supply.

### 2.2 Structure, reconvergence, trend

- Spells (consecutive triggered buckets, DTE>=1, 2.5c): ~138 organic
  spells/month, median length 2 buckets (~1h). At 1.5c spells chain (~37/mo
  but week-long) -- bucket counts are the robust unit.
- Reconvergence (episode peak back to within 1c of the 30m pre-baseline,
  6h cap, 5m grid): median 40-55 min at 2.5-3.5c; DTE>=7 slice 50-55 min.
  16-40% of episodes never reconverge within 6h (highest at 3.5c and long
  DTE -- many large moves are informative repricings, not dislocations).
  Scheduled vs organic medians are similar (40 vs 45 min at 2.5c) -- macro
  prints do not fade meaningfully faster than organic moves on this tape.
  (The two-print FOMC preview in the 06-11 probe showed 19-35 min; this
  broader census says those were fast ones.)
- 1m refinement: 116/120 sampled 5m-detected windows confirm at 1m
  granularity. On 60 random *quiet* near-money contract-days, a 1m sweep
  finds missed triggers on 40% of days at 1.5c, 10% at 2.5c, 8% at 3.5c:
  the 1.5c row is a substantial lower bound; 2.5c/3.5c rows are ~10%
  undercounts. Sub-5-minute jumps >= 2.5c inside a single candle are rare
  (~1.75 contract-days/month) -- displacement is drift-paced over minutes,
  not gap-paced.
- Trend: per-bucket displacement supply (at fixed theta, DTE>=7) has been
  roughly stable since late-2024 (monthly organic counts 200-350 at 2.5c
  with vol-regime wiggle, peak 2025-07..2026-02); the dramatic growth is in
  the CATALOG (intraday/daily ladders exploding from mid-2025), which
  multiplies harvestable surface mostly at short DTE. The bucket-OR metric
  scales with the number of simultaneously-listed near-money contracts --
  treat cross-era comparisons as regime descriptions, not vol measurements.
- Edge artifacts: 2026-05/06 understate (closed-markets-only inventory: a
  market closing in July-2026 contributes no DTE>=7 coverage to May/June
  yet); 2026-06 DTE>=7 row is empty for the same reason.

## 3. P2 -- settlement calibration

Pre-registered: resolved markets, YES price sampled at DTE 1..42d (daily
grid D) and 4h/8h/16h (final-day grid H), snap to nearest 5m candle within
+-30m, keep 0.02 <= p <= 0.98, 0.05 buckets, Wilson 95% CIs; verdict zone =
priced 0.60-0.80. If they settle YES materially BELOW price -> MARKET-WRONG;
in line -> OUR-MODEL-WRONG.

### 3.1 Data forensics first (placeholder candles)

Polymarket price history seeds a new token with a flat constant-price run
(observed at 0.50 AND 0.495) until its first real trade. These placeholder
candles produced a fake 4-6x sample-count spike in the 0.45-0.55 buckets
(e.g. bucket 0.45: settle 0.277 vs price 0.484, n~=n_markets, dominated by
rows with zero prior real candles). Rule applied: drop samples on the
leading identical-flat-candle run (NOPREFIX; removes 5,602 of 36,825 D
samples, 15.2%). The 0.45-bucket anomaly then vanishes (-20.7pp -> -0.9pp).
The 0.60-0.80 verdict zone contains no such candles by construction and is
identical under all filters. Both raw and filtered tables are in the
artifact JSON; nothing else was excluded.

### 3.2 Calibration curve (D grid, NOPREFIX), mid-curve and tails

| bucket | n     | mkts | mean px | settle YES | Wilson 95%      | gap     |
|--------|-------|------|---------|------------|-----------------|---------|
| 0.40   | 741   | 580  | 0.422   | 0.389      | [0.354, 0.424]  | -3.4pp  |
| 0.45   | 635   | 504  | 0.473   | 0.465      | [0.426, 0.503]  | -0.9pp  |
| 0.50   | 663   | 514  | 0.521   | 0.460      | [0.422, 0.498]  | -6.1pp  |
| 0.55   | 456   | 373  | 0.572   | 0.581      | [0.535, 0.626]  | +0.9pp  |
| 0.60   | 494   | 383  | 0.623   | 0.611      | [0.568, 0.653]  | -1.1pp  |
| 0.65   | 542   | 411  | 0.673   | 0.644      | [0.603, 0.683]  | -2.9pp  |
| 0.70   | 548   | 430  | 0.723   | 0.717      | [0.678, 0.753]  | -0.6pp  |
| 0.75   | 532   | 425  | 0.773   | 0.729      | [0.690, 0.765]  | -4.4pp  |
| 0.80   | 585   | 455  | 0.824   | 0.762      | [0.726, 0.795]  | -6.1pp  |
| 0.85   | 789   | 575  | 0.874   | 0.803      | [0.774, 0.830]  | -7.0pp  |
| 0.90   | 1,140 | 734  | 0.926   | 0.889      | [0.870, 0.906]  | -3.7pp  |
| 0.95   | 1,474 | 803  | 0.967   | 0.945      | [0.932, 0.956]  | -2.1pp  |

Tails (NOPREFIX): priced 0.80-0.98 pooled gap **-4.2pp** (n=4,017) -- the
high-price favorite zone is consistently rich, which independently rhymes
with the 0.99-wall penny-harvest finding; priced <0.50 pooled gap -2.5pp
(YES longshots also slightly rich). Every bucket gap on the curve is <= 0
except 0.55: the YES side is systemically rich across the curve.

### 3.3 Verdict zone and its anatomy

| slice (D grid unless noted)            | n     | mean px | settle YES | Wilson 95%      | gap     |
|----------------------------------------|-------|---------|------------|-----------------|---------|
| 0.60-0.80 pre-registered (all samples) | 2,240 | 0.700   | 0.669      | [0.649, 0.688]  | **-3.1pp** |
| 0.60-0.80 NOPREFIX                     | 2,116 | 0.700   | 0.677      | [0.657, 0.696]  | -2.3pp  |
| 0.50-0.80 NOPREFIX                     | 3,235 | 0.645   | 0.619      | [0.602, 0.635]  | -2.6pp  |
| ... DTE 1-3                            | 1,213 | 0.650   | 0.674      | [0.647, 0.699]  | +2.4pp  |
| ... DTE 4-14                           | 1,749 | 0.646   | 0.598      | [0.574, 0.620]  | **-4.8pp** |
| ... DTE 15-42                          | 273   | 0.619   | 0.513      | [0.454, 0.572]  | -10.6pp |
| one-per-market, DTE 4-14, 0.60-0.80    | 320   | 0.700   | 0.663      | [0.609, 0.712]  | -3.7pp  |
| one-per-market, DTE 4-14, 0.50-0.80    | 474   | 0.649   | 0.601      | [0.557, 0.644]  | -4.7pp  |
| H grid (final day), 0.60-0.80          | 843   | 0.698   | 0.727      | [0.696, 0.756]  | +2.9pp  |

Families (0.50-0.80, NOPREFIX): terminal -4.8pp, one_touch -1.5pp,
terminal_intraday -3.1pp. Eras: 2024H2 +7.5pp and 2025H1 +21.6pp (tiny
universes, 42-80 markets, BTC trending -- one-touch "reach" legs hit), then
**2025H2 -7.2pp / 2026H1 -4.2pp** (n~2,800 each) in the modern dense
catalog -- the same era the bias-footprint probe measured.

**Pre-registered verdict: MARKET-WRONG, with a DTE qualifier.** Mid-life
(DTE>=4) YES prices in the 0.5-0.8 zone settle 3-7pp below price; the
pooled 0.60-0.80 CI excludes the price under both the raw and
artifact-filtered samples, the cluster-robust one-per-market control in the
band where the premium lives agrees in sign and size (-3.7pp), and the
modern-era slices are the most negative. At DTE 1-3 the market is
calibrated (sign flips slightly positive) and on the final day YES is
mildly cheap. This is exactly the shape the bias-footprint probe implied
(+4.5pp YES premium vs SVI fair at fair 0.50-0.80, buy_no carrying the
edge): the premium is real AT THE MID LEVEL, it lives in the weeks before
expiry, and it has already decayed by the final days. Caveats against
over-reading: settlement frequencies are gross of fees (PM crypto taker
0.07 since ~Mar-2026), depth- and queue-blind, and serially correlated
within markets (pooled CIs overstate precision; the one-per-market rows are
the honest width). Nothing here demonstrates that a NO fill at these mids
was attainable (Q1).

## 4. Pointers

- Scripts + raw JSON: `outputs/pm_history/` (p0*-p6*, inventory.jsonl,
  plan.jsonl, candles5m/ ~1.1GB, census_results.json, refine_results.json,
  calib_results.json, macro_calendar.json) -- gitignored, rebuildable
  (~9k pmxt calls, ~40 min).
- New pmxt defect for the gotchas list: closed-status market search
  self-DoSes the sidecar throttler (paginateSearchParallel); use Gamma REST
  for historical discovery, pmxt fetch_ohlcv (chunked: 1m<=7d, 5m<=14d) for
  history.

## END STATE

- P1 and P2 both delivered; surprises documented inline (placeholder-candle
  forensics; census metric replaced mid-flight with a monotone one; FOMC
  2026-05-06 phantom-date correction for the 06-11 report).
- pytest tripwire: `py -3.12 -m pytest -q` -> see commit block below.
- Working tree: docs/diag_pm_history_2026-06-13.md added; all other
  artifacts gitignored under outputs/.
