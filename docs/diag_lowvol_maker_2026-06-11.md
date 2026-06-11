# Low-vol maker diagnostic: passive-quoting feasibility on the banked quiet windows

Date: 2026-06-11. Companion to the LOW-VOL TRACK section in docs/todo.md
(lead candidate: options-informed passive quoting). Read-only analysis;
zero source changes; no ledger writes anywhere.

> **No queue position, no fee model, no inventory dynamics. Upper-bound
> feasibility evidence, not P&L. A maker build decision still requires the
> Stage 4 branch.**

## Method

Component harness over the banked recordings (the outputs/diag_repro
pattern; the agent pipeline was NOT run -- no ./paper_ledger writes). The
probe mirrors the replay decode exactly (per-source gzip frame readers with
truncated-gzip tolerance, k-way merge, 5s scan boundaries anchored at first
frame ts, feeds' own `_build_tick` functions -- no second parser) and the
live cache build (`VolSurface.update` -> `DigitalPricer.price_from_surface`
-> `ProbabilityCache.update`, minus the observe-only no-arb layer), then
matches PM contracts with `ContractMatcher` defaults and records, per 5s
boundary per matchable contract: both venues' RAW book sides + the
SVI-implied fair value oriented to the contract's YES side via
`edge._model_yes_prob`. Key enabler: the resting-bid (maker) side of both
venues' books IS present in the raw recorded frames -- Polymarket `/book`
bids, Kalshi `orderbook_fp.yes_dollars`/`no_dollars` -- even though the
live tick pipeline deliberately never emits it (see the `_derive_ask_depth`
docstring). No source change was needed to measure the maker side
retroactively.

Scripts and datasets (gitignored): `outputs/_lowvol_extract.py`,
`outputs/_lowvol_p2.py`, `outputs/_lowvol_p345.py`;
`analysis_out/lowvol_maker/rows_<date>.parquet` + `inventory_<date>.json`
+ `SCHEMA.md`; full tables in `outputs/lowvol_p2_book_landscape.md` and
`outputs/lowvol_p345_analysis.md` (+ `lowvol_p345_summary.json`).

Verification: every headline number below was independently recomputed by
a second implementation (no shared code) and reproduced exactly, including
one end-to-end raw-frame spot check (a recorded Polymarket /book frame
decoded by hand matches the dataset row at machine precision). Frame
counts cross-validate against the overnight sweep replays exactly (42,109
on 0530; 702,528 on 0601; 244,805 on 0609).

## Data inventory

| window | hours (UTC) | frames | scan boundaries | rows | notes |
|---|---|---|---|---|---|
| 2026-05-30 | 18+20 | 42,109 | 1,818 | 1,245 | two ~90s sub-windows 2.5h apart |
| 2026-06-01 | 18-20 | 702,528 | 1,180 | 22,895 | h20 truncated (kalshi empty, deribit/pm mid-gzip) |
| 2026-06-09 | 18-19 | 244,805 | 507 | 3,157 | h18 truncated; 2080s in-window gap |
| 2026-06-11 | 01+02+20 | 1,070,348 | 13,899 | 24,290 | partials; h01-02 = live drill (~147s outage gaps) |

Matched contracts (distinct, per venue; `matched` = ContractMatcher
success at >=1 boundary):

| window | kalshi matched (product types) | polymarket matched (product types) |
|---|---|---|
| 2026-05-30 | 2 (one_touch=2) | 17 (terminal=7, range=7, one_touch=3) |
| 2026-06-01 | 0 | 17 (terminal=10, range=5, one_touch=2) |
| 2026-06-09 | 0 | 12 (terminal=6, range=5, one_touch=1) |
| 2026-06-11 | 0 | 31 (terminal=15, range=10, one_touch=6) |

**Kalshi formed ZERO matched terminal contracts in all four windows** (its
only matches anywhere are 0530's two one_touch KXBTCMINMON tickers). The
fair-anchored phases (P3-P5) are therefore Polymarket-only -- one more
window of evidence for the terminal-Kalshi open question, now from the
maker side: an options-fair-anchored Kalshi maker has nothing in-mandate
to quote on these tapes.

## P2 -- book landscape (all matchable contracts, per venue)

Spreads in cents; depth = share/contract count as USD-notional proxy
(NOTE: on deep-OTM books, share count overstates posted capital -- a 207k
YES bid at $0.01 is ~$2k).

Kalshi (resting YES bids vs derived YES asks):

| window | n rows | med spread (p90) | med bid@touch | med ask@touch | med bid w/1c | med ask w/1c |
|---|---|---|---|---|---|---|
| 2026-05-30 | 240 | 1.00c (1.00c) | 15,894 | 500 | 23,781 | 19,840 |
| 2026-06-01 | 8,184 | 1.00c (9.00c) | 704 | 567 | 1,110 | 6,291 |
| 2026-06-09 | 814 | 1.00c (2.00c) | 1,964 | 1,212 | 4,768 | 3,622 |
| 2026-06-11 | 5,461 | 1.00c (2.00c) | 544 | 2,000 | 2,985 | 3,071 |

Polymarket:

| window | n rows | med spread all (matched) | med bid@touch | med ask@touch | med bid w/1c | med ask w/1c |
|---|---|---|---|---|---|---|
| 2026-05-30 | 1,005 | 1.00c (0.30c) | 339 | 120 | 2,561 | 2,205 |
| 2026-06-01 | 14,711 | 0.60c (0.30c) | 627 | 200 | 2,720 | 3,385 |
| 2026-06-09 | 2,343 | 1.00c (1.60c) | 222 | 567 | 4,600 | 6,343 |
| 2026-06-11 | 18,829 | 1.00c (1.00c) | 360 | 356 | 6,306 | 6,416 |

Reading: quiet-regime books are TIGHT at the touch (median 0.6-1c both
venues, all windows) but THIN there (median touch size a few hundred USD
proxy), with 5-20x more size within 1c. This is the same geometry the
taker measurement walked into from the other side: the 16-18pp haircut
is not in the touch spread, it is in completing $200 size beyond a thin
touch into the far wall. A maker earns that structure by RESTING where
the taker pays it; the touch itself offers ~1c, not 16pp. The 0530
kalshi bid/ask asymmetry (15,894 vs 500) is real but driven by three
deep-OTM unmatched KXBTCMAX150 contracts quoted at 1-7c (share-count
proxy effect above).

## P3 -- quote placement (matched terminal rows; Polymarket-only universe)

Universe: 228 / 4,037 / 570 / 5,009 matched-terminal scan-rows
(0530/0601/0609/0611; 7/10/6/15 contracts; total n=9,844 boundaries).
Quote convention (shared with P4/P5): at each boundary t, maker BID at
`fair_mid - c` and maker ASK at `fair_mid + c`, c in {1,2,3}c, re-pegged
every 5s boundary. Classes in priority order: off_scale (q outside
[0,1]), marketable (would TAKE: bid >= best ask / ask <= best bid),
inside (strictly improves the touch), at_touch (joins best within 0.5c),
behind (deeper than best by >0.5c).

Aggregate (% of n=9,844 quotes per side per offset):

| offset | side | marketable% | inside% | at_touch% | behind% | off_scale% |
|---|---|---|---|---|---|---|
| 1c | ask | 36.1 | 17.4 | 13.9 | 27.4 | 5.3 |
| 1c | bid | 0.0 | 6.7 | 11.3 | 60.6 | 21.3 |
| 2c | ask | 28.0 | 12.4 | 4.8 | 45.0 | 9.9 |
| 2c | bid | 0.0 | 0.5 | 0.5 | 62.4 | 36.7 |
| 3c | ask | 15.7 | 11.8 | 8.3 | 54.5 | 9.9 |
| 3c | bid | 0.0 | 0.0 | 0.2 | 51.3 | 48.5 |

Per-window detail is in `outputs/lowvol_p345_analysis.md`; the pattern
holds in every window (e.g. 0609 1c ask: 53.5% marketable, 33.3% inside;
0530 bids: 0% inside/at_touch at every offset).

Reading: the two sides are structurally different because fair_mid sits
AT OR BELOW the market's best bid on 64.7% of universe boundaries (the
market prices these contracts above options fair -- the same PM-premium
the taker thesis measures as positive would-be edge). So a passive ASK
at fair+c frequently lands inside the spread or is outright marketable
(it would take, not make), while a passive BID at fair-c is buried
behind the touch or off-scale (fair near 0/1 makes fair-c leave [0,1]).
An options-fair-anchored maker on these tapes is effectively a
one-sided (ask) quoter unless it re-anchors to the book.

## P4 -- cross frequency (UPPER BOUND on fill opportunity)

Only non-marketable, non-off_scale quotes set at boundary t are
evaluated, at the same contract's next boundary t' (dt <= 30s; never
across recording gaps). Bid crossed: best_ask(t') <= q_bid(t); ask
crossed: best_bid(t') >= q_ask(t). Events = transitions into the crossed
state. This is an upper bound: no queue position is modeled (size ahead
at the same price absorbs flow first), and the 5s snapshot cadence
misses intra-interval touches while assuming any touch fills.

Aggregate:

| offset | side | eval pairs | events | contract-hrs | events/ct-hr |
|---|---|---|---|---|---|
| 1c | ask | 5,744 | 14 | 8.0 | 1.75 |
| 1c | bid | 7,687 | 0 | 10.7 | 0.00 |
| 2c | ask | 6,073 | 6 | 8.4 | 0.71 |
| 2c | bid | 6,188 | 0 | 8.6 | 0.00 |
| 3c | ask | 7,274 | 12 | 10.1 | 1.19 |
| 3c | bid | 5,029 | 0 | 7.0 | 0.00 |
| 1c | both | 13,431 | 14 | 18.7 | 0.75 |
| 2c | both | 12,261 | 6 | 17.0 | 0.35 |
| 3c | both | 12,303 | 12 | 17.1 | 0.70 |

Per window (events only): 0530 = 1 (2c ask), 0601 = 1 (3c ask), 0609 = 4
(1c ask, 11.08/ct-hr on n=260 pairs), 0611 = 26. ALL 32 events are
ask-side; 0 bid crosses over 18,904 evaluated bid pairs.

Reading: a fair-anchored resting quote RARELY interacts in quiet regime
-- pooled upper bound 0.35-0.75 events per contract-hour, and what does
interact is entirely the market bid rising to fair+c (never the ask
falling to fair-c). Activity is non-stationary: 26 of 32 events come
from the 0611 partials, and 0601 (the longest contiguous window) is
nearly dead at 1 event in ~20 contract-hours.

## P5 -- adverse-selection proxy (around each P4 cross)

Per cross event (quote q set at t), fair_mid sampled at t+h, h in
{30,60,300}s (nearest row within +/-10s, else skipped). PICKED OFF =
fair moved through the quote (ask side: fair_mid(t+h) > q_ask); CAPTURED
otherwise. Markout (ask side) = (q_ask - fair_mid(t+h)) * 100 cents;
positive = ahead of fair.

| offset | horizon | events (n) | skips | pickoff | capture | ratio | median markout |
|---|---|---|---|---|---|---|---|
| 1c | 30s | 14 | 0 | 0 | 14 | 0.00 | +1.12c |
| 1c | 60s | 14 | 0 | 0 | 14 | 0.00 | +1.09c |
| 1c | 300s | 14 | 6 | 0 | 8 | 0.00 | +1.26c |
| 2c | 30s | 6 | 0 | 0 | 6 | 0.00 | +1.97c |
| 2c | 60s | 6 | 1 | 0 | 5 | 0.00 | +1.91c |
| 2c | 300s | 6 | 4 | 0 | 2 | 0.00 | +0.54c |
| 3c | 30s | 12 | 0 | 0 | 12 | 0.00 | +2.99c |
| 3c | 60s | 12 | 0 | 0 | 12 | 0.00 | +3.02c |
| 3c | 300s | 12 | 2 | 0 | 10 | 0.00 | +3.24c |

Reading: zero pickoffs in all 83 classified event-horizons -- and the
median markout sits almost exactly at the offset c, meaning fair_mid was
essentially UNCHANGED through the cross: the market bid rose to the
quote while the options-implied fair stayed put. That is
spread-capture-shaped, not pickoff-shaped. But n=32 events is a tiny
sample concentrated in one window; this is consistent with low adverse
selection in quiet regime, it does not bound it. (The 2c/300s cell is
n=2 -- not meaningful.)

## GO/NO-GO-shaped summary (feeding the Stage 4 negative branch)

This diagnostic does NOT make the build decision (see the epistemic
header). It shapes it as follows.

What the quiet tapes SUPPORT:
- The maker-relevant book side is fully recoverable from the banked
  recordings with zero source changes -- the strategy's primary dataset
  already exists and grows with every capture window.
- The structural revenue pool is confirmed geometry: tight-but-thin
  touch (median 0.6-1c spread, hundreds at touch) backed by 5-20x size
  within 1c -- exactly the structure whose taker side measured as the
  16-18pp haircut. The maker thesis is the complement of a measured
  fact, not a hope.
- The adverse-selection proxy, as far as it goes (n=32 events, zero
  pickoffs, markout ~= offset at 30-300s), is the GOOD outcome: crosses
  came from flow reaching the quote, not from fair moving through it.

What the quiet tapes COUNT AGAINST (or leave open):
- Fair-anchored passive quotes RARELY interact: pooled upper bound
  0.35-0.75 crosses/contract-hour (before queue, before fees), almost
  all from one window (0611: 26/32), with the longest contiguous window
  (0601) nearly dead. At face value, a fair+/-c re-pegging maker does
  very little business in quiet regime on Polymarket.
- The interaction is ONE-SIDED: fair_mid sits at/below the market bid
  on ~65% of boundaries, so the bid quote never trades (0 crosses in
  18,904 pairs) and inventory would accumulate short-YES. Inventory
  dynamics -- explicitly out of scope here -- become the first-order
  question.
- The LEAD venue is not measurable: Kalshi formed zero matched terminal
  contracts in any window. An options-fair-anchored Kalshi maker has
  nothing in-mandate to quote on these tapes; if the terminal-Kalshi
  open question resolves to barrier-only, Kalshi maker quoting needs
  genuine barrier pricing first (the SECOND item's big lift), and the
  Polymarket numbers above are the only fair-anchored maker evidence
  available.
- Kalshi maker fee treatment remains unresolved (no rebates known);
  fees of taker-comparable size would consume offsets of this scale.

If the Stage 4 branch goes NEGATIVE and this track activates, first
build items in order: (1) emit resting-bid depth fields (the deliberate
gap -- `_derive_ask_depth` docstring); (2) a queue-position model (the
binding unknown between these upper bounds and fills); (3) the fee
answer per venue; (4) inventory policy. Items the data says NOT to
build yet: anything that assumes two-sided fair-anchored quoting works
as-is (it is one-sided on these tapes), and any Kalshi terminal-binary
maker (nothing to quote).

## Caveats (carry these with every number above)

1. P4/P5 are upper-bound, tiny-sample measurements: 32 cross events over
   ~18 contract-hours, n=83 classified event-horizons, no queue position,
   5s snapshot cadence (undercounts touches, overcounts fills-given-touch),
   quotes re-pegged every boundary.
2. The bid/ask asymmetry is a property of the fair-vs-book offset (fair at
   or below best bid ~65% of boundaries), not evidence of symmetric
   two-sided maker viability.
3. Per-window rates are non-stationary (0 to 11.08 events/ct-hr across
   cells); 26 of 32 events come from the 0611 partials.
4. Depth is share-count-as-USD-notional proxy; deep-OTM cells overstate
   posted capital (0530 kalshi 15,894-vs-500 is three unmatched
   KXBTCMAX150 books at 1-7c).
5. The SVI fair is the terminal digital P(S_T > K); one_touch/range rows
   are book-landscape evidence only (their fair is structurally wrong),
   and all Kalshi matches anywhere in these windows are non-terminal.
6. 0611 hours 01-02 include the Stage-1 live drill outage (~147s gaps);
   cross evaluation never spans gaps (dt <= 30s rule), but coverage is
   reduced.
