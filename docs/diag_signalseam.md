# Diagnosis: Signal seam for the options-implied probability

Date: 2026-06-09
Baseline: master @ a401da0 (post-PR-#2 merge), 801 tests green, tree clean.
Goal under diagnosis: introduce a frozen `Signal` dataclass
(estimated_prob, confidence, direction, signal_type='options_implied')
as a single-signal passthrough at the point where the options-implied
probability + confidence are produced and handed to the
contract-matching/edge step.

## VERDICT: HARD GATE FIRED -- STOP, do not implement

Both stop conditions in the Phase 1 hard gate hold:

1. The producer output is ALREADY a typed (frozen) object, and every
   downstream hop is typed end-to-end.
2. Probability and confidence are produced at two different pipeline
   stages, by two different components, on two different cadences.
   There is no single seam where "prob + confidence" are produced
   together and handed downstream.

Details and evidence below. No code was changed.

## 1. Where the options-implied probability is actually produced

Producer: `btc_pm_arb.pricing.digital_pricer:DigitalPricer.price_from_surface`
(src/btc_pm_arb/pricing/digital_pricer.py:101). It returns
`DigitalPrice` -- a `@dataclass(frozen=True)` defined at
src/btc_pm_arb/pricing/digital_pricer.py:76 with fields:

| field  | type  | meaning                                      |
|--------|-------|----------------------------------------------|
| bid    | float | lower probability bound (conservative)       |
| mid    | float | midpoint probability (the "estimated_prob")  |
| ask    | float | upper probability bound                      |
| method | str   | "analytical" or "call_spread"                |

That is: the producer already emits a frozen typed object. It is not a
single `estimated_prob` -- it is a (bid, mid, ask) triple, and the edge
step genuinely consumes all three bounds (conservative YES edge uses
bid, conservative NO edge uses ask; see src/btc_pm_arb/signals/edge.py:125-151).
A `Signal.estimated_prob` scalar would be a lossy narrowing, not a
faithful superset.

(`DigitalPricer.price_from_ticks`, digital_pricer.py:141, also returns
`DigitalPrice` but has no call sites in src/ -- the analytical surface
path is the only live producer.)

## 2. The handoff chain downstream (every hop already typed)

| hop | producer -> consumer | carrier type | location |
|-----|----------------------|--------------|----------|
| 1 | DigitalPricer -> cache write | `DigitalPrice` (frozen dataclass) | main.py:409 (`update_cache_from_surface`) |
| 2 | cache write -> grid | `CacheEntry` (dataclass) | main.py:412-423 -> cache.py:75 (`ProbabilityCache.update`) |
| 3 | cache -> matcher | `CacheEntry` via `ProbabilityCache.interpolate` | matcher.py:152 |
| 4 | matcher -> edge | `MatchResult.options_entry: CacheEntry` (dataclass) | matcher.py:43, consumed at edge.py:116, 125-127 |
| 5 | edge -> filter | `EdgeResult` (dataclass) | edge.py:49, main.py:462 -> main.py:482 |
| 6 | filter -> emit | `ArbitrageSignal` (pydantic) with `options_quote: ProbabilityQuote` | filters.py:524 (`_to_arbitrage_signal`), models.py:193 |

Fields carried at the boundary the goal targets (hop 2, the single
point where the pricer's output is handed downstream,
main.py:412-423):

| field     | source                            | notes |
|-----------|-----------------------------------|-------|
| strike    | surface tick strike               | grid key |
| expiry    | dirty-expiry datetime             | grid key |
| bid_prob  | DigitalPrice.bid                  | clipped to [0,1] in CacheEntry |
| ask_prob  | DigitalPrice.ask                  | clipped to [0,1] |
| mid_prob  | DigitalPrice.mid                  | clipped to [0,1] |
| source    | DataSource.DERIBIT (constant)     | |
| timestamp | self.clock.now()                  | sim-time under replay; freshness read by the stale-data gate |

NOT carried at this boundary: confidence (does not exist yet),
direction (the pricer always emits P(above); polarity mapping happens
inside the edge step via `_model_yes_prob`, edge.py:291), contract id
(no PM contract is involved yet -- this is a per-(strike, expiry) grid
write that fans out to many future matches). `DigitalPrice.method` is
dropped at hop 2 (the only field not already forwarded).

## 3. Where confidence is actually produced (a different stage)

Producer: `btc_pm_arb.signals.confidence:ConfidenceScorer.score`
(src/btc_pm_arb/signals/confidence.py:146), called from
`run_scan_pipeline` at src/btc_pm_arb/main.py:502 -- AFTER matching,
AFTER the edge computation, and AFTER the filter pass, backfilled
in-place onto each passing `ArbitrageSignal` (constructed with a 0.5
placeholder at filters.py:544). Rejected edges never get a confidence
(payload carries None, main.py:1010).

Confidence is a function of the EDGE STEP'S OWN OUTPUT, not of the
pricer's output: its five dimensions (confidence.py:118-124) read
`EdgeResult.edge_persistence` (exists only after EdgeCalculator's
history update), `MatchResult.match_quality` (exists only after
matching), PM book depth, options spread, and SVI fit RMSE. It cannot
be computed at the probability-producer boundary without changing what
it measures.

## 4. Why the hard gate applies

Gate condition A -- "producer output is already a typed object": YES.
`DigitalPrice` is already a frozen dataclass; the goal's `Signal`
(frozen dataclass with prob + meta) is shape-for-shape what
`DigitalPrice` -> `CacheEntry` -> `ProbabilityQuote` already provide.
Adding `Signal` would duplicate/rename an existing typed carrier, not
introduce a missing seam.

Gate condition B -- "prob/confidence produced in scattered places with
no single clean seam": YES. Probability is produced per (strike,
expiry) grid point at cache-update time, before any PM contract is in
scope (one write fans out to many matches). Confidence is produced per
passing signal, post-filter, from edge-step outputs. The two values
never co-exist at any producer boundary. The goal's premise ("the
options-implied probability + confidence ... handed to the
contract-matching/edge step") does not match the code: confidence is
computed downstream OF that step, and only for filter survivors.

Wiring a `Signal{estimated_prob, confidence}` passthrough would
therefore force one of:
  (a) moving confidence computation upstream of matching -- impossible
      without semantic change (its inputs do not exist yet), or
  (b) stamping a placeholder confidence at the producer -- which is
      exactly the `confidence=0.5` placeholder `ArbitrageSignal`
      already models at filters.py:544, or
  (c) collapsing the (bid, mid, ask) triple into one `estimated_prob`
      -- lossy, and would have to change the conservative-edge math.
All three are refactors, not a passthrough. Per the gate: STOP.

## 5. Replay-path note (verified, no second seam)

The replay driver reuses the identical seams -- it calls
`update_cache_from_surface` and `run_scan_pipeline` on the same agent
object (src/btc_pm_arb/feeds/replay.py:372-374). No parallel pipeline
recomputes probability or confidence; tools/ and server/ only read
already-typed values. A future Signal/aggregator attachment point, if
ever revisited after a strategy shows fill-surviving edge, would most
naturally wrap hop 4-5 (MatchResult -> EdgeResult), where per-contract
prob, direction, freshness, and contract id first co-exist -- recorded
here as observation only, per the gate no refactor was attempted.
