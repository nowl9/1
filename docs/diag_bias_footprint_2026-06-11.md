# Bias-footprint probe: the external scalping doc's priors vs the banked tape

Date: 2026-06-11. Companion to the EXTERNAL-DOC TRIAGE section in
docs/todo.md (C1 of this goal, d58f572). Read-only analysis; zero source
changes; no ledger writes; recordings untouched.

> **External doc figures are unverified priors. Quiet-regime tape only;
> depth recorded, queue position not. Nulls are findings. Nothing here
> authorizes a build -- Stage 4 still gates all strategy work.**

## Method and provenance

Four probes (P2-P5), each answerable from data we already own. P2 runs
over the archived ledgers (overnight_sweep_2026-06-09, all three windows,
plus the kalshi_depth_c3 re-replay) joined against the low-vol harness row
datasets; P3 re-walks the banked recordings book-only, importing the
decode machinery of outputs/_lowvol_extract.py verbatim (FrameReader,
_parse_kalshi_bid_levels, _derive_ask_depth, _depth_within, heapq merge,
5s boundaries, 60s freshness) -- no second parser; P4 runs entirely on the
harness rows (analysis_out/lowvol_maker/rows_<date>.parquet, schema in
SCHEMA.md there); P5 is desk research against the live Kalshi/Polymarket
fee sources plus read-only series metadata. Scripts and intermediates are
gitignored: outputs/_bias_p2_*.py, _bias_p3_*.py, _bias_p4_*.py,
_bias_p5_*.py, analysis_out/bias_p3/. Each probe independently recomputed
at least one headline number through a second code path; P3 additionally
hand-decoded a raw frame end-to-end and cross-checked frame/tick counts
against the harness inventory files exactly (all four windows).

Edge formulas were re-verified against src/btc_pm_arb/signals/edge.py
before use: edge_yes_cons = fair_bid - yes_ask; edge_no_cons = yes_bid -
fair_ask (the no_ask fallback path); side selection per _pick_best_side
(None when both <= 0, else buy_yes iff adj_yes >= adj_no).

## P2 -- BUY_NO vs BUY_YES asymmetry (the Optimism Tax prior)

PRE-REGISTERED: if the Optimism Tax (takers overbuy YES longshots)
operates on BTC threshold markets, buy_no should dominate buy_yes in
count and magnitude; if symmetric, the prior fails to transfer from
sports to our universe.

Primary answer, join-free, from the harness rows (matched terminal
boundaries with fair and book present; n=9,844, ALL Polymarket -- Kalshi
has zero matched terminal rows in every window). Edge stats over
positive-edge boundaries of that side, in pp:

| window | n | buy_yes | buy_no | none | yes med | yes p90 | yes max | no med | no p90 | no max |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-05-30 | 228 | 45 | 166 | 17 | 0.076 | 0.135 | 0.136 | 1.237 | 2.462 | 2.723 |
| 2026-06-01 | 4037 | 75 | 1576 | 2386 | 0.053 | 0.138 | 0.191 | 1.792 | 2.353 | 3.843 |
| 2026-06-09 | 570 | 0 | 379 | 191 | - | - | - | 1.567 | 3.275 | 4.179 |
| 2026-06-11 | 5009 | 633 | 3601 | 775 | 0.428 | 0.514 | 0.603 | 0.795 | 3.110 | 4.719 |
| POOLED | 9844 | 753 | 5722 | 3369 | 0.406 | 0.508 | 0.603 | 1.178 | 3.013 | 4.719 |

buy_no dominates BOTH axes: 88.4% of positive-edge boundaries (wins all
four windows, 379-0 on 0609) and median 1.178pp vs 0.406pp (p90 3.013 vs
0.508; max 4.719 vs 0.603). Contract-level robustness (boundaries are
serially correlated): of 35 distinct matched-terminal contracts, 24
produce buy_no-positive boundaries vs 6 buy_yes.

Mechanism cut by options fair_mid bucket (pooled):

| fair_mid bucket | n | buy_no share of pos | med buy_no edge pp | med (yes_mid - fair_mid) pp |
|---|---|---|---|---|
| <0.05 | 5048 | 83.4% | 0.364 | 0.383 |
| 0.05-0.20 | 1437 | 90.3% | 1.939 | 1.656 |
| 0.20-0.50 | 1067 | 100.0% | 1.478 | 2.609 |
| 0.50-0.80 | 662 | 100.0% | 3.068 | 4.468 |
| 0.80-0.95 | 294 | 100.0% | 0.873 | 1.572 |
| >0.95 | 1336 | 79.4% | 0.393 | 0.054 |

The YES-above-fair offset is NOT longshot-concentrated: it peaks
mid-curve (fair 0.50-0.80: median premium +4.47pp, 100% buy_no) and
shrinks to ~0 above 0.95. That is a broad one-sided level offset, not
the classic longshot-overbuy shape.

Ledger funnel: rejections reconcile at 292 / 2,345 / 509
(0530/0601/0609). conservative_edge rejections: 79 / 531 / 131, medians
1.18 / 1.50 / 1.34pp, maxima never above 2.98pp (the 3% floor held
everywhere). SCOPE LIMIT CONFIRMED: 0601 and 0609 rejections are 100%
Polymarket; the only Kalshi evidence in any ledger is 0530's 53
one_touch_barrier rejections (categorical gate, no edge/side content).
The kalshi_depth_c3 re-replay deduped byte-equivalent to sweep-0530
after dropping run_id/timestamp -- zero independent evidence, excluded
from pooled numbers, exactly as pre-registered at the c3 replay.

Side attribution: all 741 conservative_edge rejections joined to a
same-window same-contract harness boundary whose raw recomputed best
edge matches the recorded best_conservative_edge EXACTLY (741/741 within
1e-9). This also resolves the basis-adjustment caveat empirically: the
BasisAdjuster was a no-op for every rejected signal on this tape. Split:
700 buy_no (94.5%, median 1.43pp) vs 41 buy_yes (5.5%, median 0.075pp --
roughly 20x smaller). The walked near-floor set (fill_adjusted_edge or
chase_adjusted_edge non-null; n=455) is 100% buy_no. The single emitted
order (0601) is buy_no. (The May-era archive's 137 buy_yes Kalshi orders
are the known pre-fix depth-parser artifact, fixed 6c2d5af -- bogus
signals, not counter-evidence.)

PRE-REGISTERED vs FOUND: **partial transfer.** The fade-YES direction
transfers -- buy_no carries essentially all the edge on both
pre-registered axes -- but the textbook longshot mechanism does not: the
offset peaks at mid-probability books and vanishes above 0.95, looking
like a broad one-sided YES premium (or, indistinguishably on this data,
a systematic downward bias in our options-implied fair) rather than
taker longshot-overbuying. Established for Polymarket only; the prior is
untested on Kalshi (zero matched terminal rows). Independently confirms
the maker-diag signature: fair_mid <= market yes_bid on 64.7% of
boundaries, median yes_mid - fair_mid = +0.83pp.

## P3 -- 0.99-wall characterization (the incumbent's footprint)

Book-band walk over all four windows (one-sided and metadata-less books
kept -- wall geometry is often one-sided; probe rows therefore exceed
harness rows). Frame/tick counts match the harness inventories exactly.

Band concentration: deep-side ask mass is corner-pinned. Share of total
>=0.90 ask mass sitting exactly in [0.99,1.00): 57-87% per venue-window
(kalshi 57-87%, polymarket 65-76%); per-band medians are ZERO everywhere
except the corner (median 0.99-band size 359-11k kalshi, 35k-60k
polymarket). The mirrored bid side matches: 48-89% of (0,0.10] bid mass
sits in the penny band (0,0.01].

Wall vs near-touch (wall = ask size at >= 0.97; near = ask size within
2c of best ask; universe = rows with best_ask < 0.90 -- the geometry the
taker book-walker hit):

| window | venue | n | wall>0 | med ratio | p90 ratio | med wall | med near |
|---|---|---|---|---|---|---|---|
| 2026-05-30 | kalshi | 816 | 72.9% | 0.02 | 0.82 | 412 | 17412 |
| 2026-05-30 | polymarket | 1249 | 100.0% | 14.95 | 131.02 | 82706 | 7156 |
| 2026-06-01 | kalshi | 9092 | 100.0% | 2.18 | 32.60 | 17731 | 8360 |
| 2026-06-01 | polymarket | 15110 | 100.0% | 16.88 | 431.07 | 83440 | 8046 |
| 2026-06-09 | kalshi | 1110 | 100.0% | 1.96 | 18.38 | 10276 | 6122 |
| 2026-06-09 | polymarket | 2340 | 100.0% | 6.73 | 199.08 | 50072 | 6833 |
| 2026-06-11 | kalshi | 9407 | 100.0% | 1.73 | 15.16 | 10464 | 7998 |
| 2026-06-11 | polymarket | 18287 | 99.5% | 9.07 | 108.80 | 72242 | 7111 |

On Polymarket the far wall is typically 7-17x near-touch depth (p90
100-430x). When present, the 0.99 wall's median distance from best ask
is 93 cents (p10: 40c) -- it is essentially never near fair. (0530
kalshi median 0.02 is the outlier: mostly small walls that day plus one
1.12M monster.)

Expiry dependence: the wall is INDISCRIMINATE across expiry. The largest
median walls sit in the >7d bucket on both venues (polymarket >7d median
350k -- the biggest of any bucket; kalshi >7d 96.8% presence). It does
NOT concentrate near expiry as the doc's favorite-collection narrative
predicts.

Persistence: 221 contracts ever carry a 0.99-band ask; 94.6% have it at
>= 500 proxy units at EVERY observed boundary; 99.5% have it > 0 at
every boundary; median number of on/off runs per contract = 1. One
Polymarket one_touch contract (BTC >= 90k by 2027-01-01) carries a
651-661k wall in ALL FOUR windows -- a multi-week immovable fixture.
Verdict: persistent fixture, not flicker.

Reading: the wall is real, enormous, corner-pinned, and permanent -- but
it is NOT the doc's near-expiry favorite-quoter. It sits at the extreme
tick (median best_ask of a wall-carrying row is 0.06; overwhelmingly
longshot one_touch books), at all expiries alike. Economically a resting
YES-ask at 0.99 on a longshot IS a penny bid for the favorite side (on
Kalshi literally a NO bid at $0.01; on Polymarket, mint-and-offer): the
owner risks ~1c to collect ~99c from any taker who walks the thin
near-touch book into it. That is the other side of our measured 16-18pp
taker haircut -- the haircut is the toll our walker pays this fixture.
Genuine near-touch favorite quoting (best_bid >= 0.97) exists only on
Polymarket in these windows (3,355 rows, median tte ~69h) and is ABSENT
from the Kalshi tape (zero best_bid >= 0.90 rows). Proxy caveat:
share-count-as-USD grossly overstates posted capital at the corner --
the 1.12M Kalshi wall is ~$11k actually posted.

## P4 -- OIB predictiveness

PRE-REGISTERED: no expectation; discovery. An honest null is a
first-class result.

Method: OIB_t = (B-A)/(B+A) on depth within 1c and within 2c of touch.
HONEST DEVIATION: the goal spec asked for 1c and 3c bands, but the
harness rows carry 1c and 2c aggregates; we use 2c as the wider band
rather than re-extracting. mid = (best_bid_raw + best_ask_raw)/2;
horizons +60/+300/+900s, future mid from the same contract's row at ts+h
(+/-10s, skip if absent -- never bridges the 0530/0609/0611 recording
gaps); liquidity floor min(B,A) >= 100 proxy units in the tested band
(the doc's $100-notional prior; 43,851 of 51,587 rows pass at 1c). Ties
(zero mid change) dominate the quiet tape -- 57-100% of pairs -- and are
excluded from hit rates but reported.

Pooled per venue (hit = sign agreement over nonzero-OIB nonzero-move
pairs; rho = Spearman over all floor-passing pairs):

| venue | band | h | n | tie_sh | n_dir | hit | rho | Q5-Q1 (c) |
|---|---|---|---|---|---|---|---|---|
| kalshi | 1c | 60 | 12010 | 0.967 | 395 | 0.630 | 0.044 | 0.02 |
| kalshi | 1c | 300 | 9890 | 0.889 | 1094 | 0.695 | 0.102 | 0.12 |
| kalshi | 1c | 900 | 7118 | 0.794 | 1469 | 0.774 | 0.193 | 0.37 |
| kalshi | 2c | 60 | 13673 | 0.967 | 445 | 0.676 | 0.049 | 0.03 |
| kalshi | 2c | 300 | 11367 | 0.890 | 1255 | 0.703 | 0.098 | 0.11 |
| kalshi | 2c | 900 | 8078 | 0.780 | 1774 | 0.660 | 0.149 | 0.29 |
| polymarket | 1c | 60 | 28824 | 0.803 | 5619 | 0.569 | 0.069 | 0.04 |
| polymarket | 1c | 300 | 23280 | 0.686 | 7206 | 0.538 | 0.062 | 0.07 |
| polymarket | 1c | 900 | 16675 | 0.585 | 6811 | 0.519 | 0.052 | 0.11 |
| polymarket | 2c | 60 | 32812 | 0.802 | 6363 | 0.574 | 0.048 | 0.03 |
| polymarket | 2c | 300 | 26600 | 0.675 | 8386 | 0.546 | 0.036 | 0.04 |
| polymarket | 2c | 900 | 19139 | 0.579 | 7870 | 0.532 | 0.008 | 0.01 |

Robustness slice (terminal-only) CHANGES the Kalshi conclusion: the
pooled Kalshi positives (hit up to 0.88, rho up to 0.31 on 0611 cells)
are carried by the one_touch product on a few contracts in one window
(12,872 of 14,699 kalshi rows; 8 of 9 contracts on 0611); the Kalshi
terminal-only slice is 1,827 rows from 3 contracts and INVERTS (hit
0.158-0.420). Polymarket terminal-only matches its full slice.

FOUND: **a fragile near-null with a weak short-horizon positive on
Polymarket only.** Polymarket 60s: hit ~0.57, rho ~0.05-0.07, top-minus-
bottom OIB quintile spread 0.03-0.04 cents, directionally consistent in
3 of 4 windows and both bands, decaying -- or sign-flipping -- by 900s.
Even the most charitable cells put the quintile spread at 0.02-0.4
cents: under one tick, far below 3% MIN_EDGE economics. Caveats:
overlapping observations on a 5s grid mean effective sample is roughly
h/5 smaller than n (nominal p-values decorative); share-count depth
proxy; quiet regime only. At best OIB is timing/queue information, never
direction: weakly supports at most a low-priority, Stage-4-gated look as
a Polymarket-only 60s-horizon ConfidenceScorer/timing tiebreak input.
The Kalshi evidence is too concentrated to support anything.

## P5 -- fee resolution (closes the maker diag's open question)

Sources: official Kalshi fee schedule PDF eff. 2026-02-05 (live URL
bot-walled since ~Mar 2026; recovered byte-identical via Wayback
2026-02-18) cross-checked against LIVE Kalshi trade-API public metadata
fetched 2026-06-11 (10,836 series scanned via fee_type/fee_multiplier;
plus GET /series/fee_changes history 2025-10-04..2026-06-08), Kalshi
help center, and official Polymarket fee docs fetched 2026-06-11.

CONFIRMED-CURRENT:
- Kalshi taker: roundup(0.07 x C x P x (1-P)) dollars, on marketable
  executions only. Per-series variations: S&P/Nasdaq series at half rate
  (fee_multiplier 0.5, exactly 9 series); 13 series fee-free including
  KXBTCY (waived 2026-02-26) and the new perp series (2026-06-03).
- Kalshi maker: roundup(0.0175 x C x P x (1-P)), execution-only, cancels
  free, ONLY on the 130 of 10,836 series with fee_type
  quadratic_with_maker_fees (mostly sports; plus KXCPI/KXFED/KXPAYROLLS
  etc.). RESIDUAL RESOLVED: every core BTC price series we would quote
  -- KXBTCD, KXBTC, KXBTC15M, KXBTCMAXW/M/Y, KXBTCMAX100 -- is plain
  quadratic, i.e. maker fee ZERO today. Exactly two BTC milestone series
  (KXBTCMAX125, KXBTCMAX150) ARE maker-fee listed, so flips are
  possible: re-poll GET /series/fee_changes before any go-live. The
  fee-change log shows zero pending/historical changes for any KXBTC
  binary.
- Kalshi settlement fee: zero. No membership fee.
- Polymarket: the banked belief "zero trading fee" is STALE. Current
  official schedule: fee = C x rate x P x (1-P) with CRYPTO TAKER RATE
  0.07 (sports 0.03, finance/politics 0.04, economics 0.05, geopolitics
  0); makers never charged, plus 20% maker rebates on crypto; 5-decimal
  rounding (no cent roundup). Rollout Jan 2026 (high-frequency crypto),
  Fee Structure V2 eff. ~2026-03-30 (effective dates from secondary
  sources only -- the one UNRESOLVED-grade caveat).

Net-of-fee math (cents per contract):

(a) Maker offsets (the maker diag's 1/2/3c). Actual today: KXBTC* not
maker-listed, fee 0 -> net = gross 1.00/2.00/3.00 at every price. Under
the adverse flip scenario (maker-listed), at C=100:

| P | maker fee c/ct | net 1c | net 2c | net 3c |
|---|---|---|---|---|
| 0.05 | 0.09 | 0.91 | 1.91 | 2.91 |
| 0.10 | 0.16 | 0.84 | 1.84 | 2.84 |
| 0.20 | 0.28 | 0.72 | 1.72 | 2.72 |
| 0.50 | 0.44 | 0.56 | 1.56 | 2.56 |

Cent roundup makes the 1-lot worst case 1c/ct (the whole 1c offset) --
size maker clips at >= 50-100 contracts; a >$10/month rounding
reimbursement softens this.

(b) The doc's longshot example (5c mispricing, Kalshi taker formula):

| P | fee c/ct C=100 | net of 5c | % of 5c eaten (C=100 / C=1) |
|---|---|---|---|
| 0.05 | 0.34 | 4.66 | 6.8% / 20.0% |
| 0.10 | 0.63 | 4.37 | 12.6% / 20.0% |
| 0.39 | 1.67 | 3.33 | 33.4% / 40.0% |
| 0.50 | 1.75 | 3.25 | 35.0% / 40.0% |

The doc's "taker fee eats ~1/3 of the 5c" holds only near P ~= 0.39 (or
0.61). At true longshot prices the drag is 6.8-12.6% on 100-lots -- the
doc overstates fee drag there 2-5x (while cent roundup pins 1-lots at
20%).

(c) Our existing taker leg: the recorded sweep order is buy NO at 0.81
on Polymarket ($200 ~= 247 contracts; the only emitted order on these
tapes). At P=0.81: Polymarket crypto taker (actual rule today) ~1.077
c/ct ($2.65 on the clip); hypothetical Kalshi taker ~1.081 c/ct --
effectively identical now. ~1.08c/ct consumes ~35% of a 3c adjusted
edge. NOTE: the repo's fill simulator currently records fees_usd = 0.0
unconditionally (execution/fill_simulator.py:384, default never
overridden) -- every banked fill-adjusted number is gross-of-fee, which
was correct under the old PM schedule and is now optimistic by ~1c/ct on
crypto taker legs. Flagged in docs/todo.md OPEN ITEMS; src untouched
this goal.

## Synthesis -- what the tape did to each prior

| Triage row (todo.md) | Prior | Tape verdict |
|---|---|---|
| Longshot-fade (on options-arb) | Takers overbuy YES longshots; fade carries edge | PARTIAL TRANSFER: buy_no carries ~all edge (88% of boundaries, 94.5% of rejections, 100% of walked set) but the premium peaks mid-curve, not in longshots; PM-only evidence |
| Near-expiry favorite collection | 90c+ near-expiry favorites maker-positive | REFRAMED: the 0.99 wall is real, permanent, and expiry-INDISCRIMINATE -- a deep-tick penny harvest, not near-expiry favorite quoting; near-touch 0.97+ quoting exists only on PM and not at all on Kalshi tape; row stays excluded/record-only |
| OIB confidence/timing dimension | OIB predicts short-horizon mids | FRAGILE NEAR-NULL: weak PM-only 60s effect (hit ~0.57, <1 tick), Kalshi effect fails robustness; at most a Stage-4-gated PM timing tiebreak |
| Maker LEAD fee question | (open question, not doc prior) | RESOLVED: KXBTC* maker fee zero today; offsets survive even adverse flip (0.09-0.44c at C=100); re-poll fee_changes pre-go-live |
| Doc's +1.12%/trade maker claim | Optimism Tax funds makers | UNTESTABLE on our tape directly (no queue position, no sports data); the structural revenue pool it implies IS consistent with the measured 16-18pp haircut geometry, but attribution to "optimism" is not supported -- the offset is a broad YES premium |
| Sports/Ent category expansion | 2.23-4.79pp maker gap there | UNTESTED -- no sports data banked; stays a data-only post-FOMC candidate |

Discoveries the priors did not predict: (1) Polymarket now charges
quadratic taker fees at the same 0.07 crypto rate as Kalshi -- the
fee-free-PM assumption embedded in every banked fill-adjusted number is
stale by ~1.08c/ct at our recorded fill price; (2) the BasisAdjuster was
a no-op on every rejected signal on these tapes (741/741 exact raw-edge
matches); (3) the wall's mirrored penny-bid side is the same fixture
seen from the NO side.

## Caveats (carry with every number)

1. Quiet-regime tape only: four windows, ~6 distinct trading hours each,
   recording gaps in three of them (0530 8,907s; 0609 2,080s; 0611
   ~147s drill outages). Forward-looking joins never bridge gaps.
2. Depth is share-count-as-USD-notional proxy; corner bands grossly
   overstate posted capital (a 1.12M wall at 0.01 is ~$11k).
3. P2's side attribution and magnitudes are Polymarket-only; Kalshi
   terminal evidence does not exist on these tapes (the standing
   terminal-Kalshi open question).
4. P4's serial correlation: 5s grid with 60-900s horizons -- effective
   sample ~h/5 smaller than n; significance read qualitatively.
5. P5's two date caveats: live Kalshi PDF unverifiable byte-for-byte
   since ~Mar 2026 (Wayback copy + live API used instead); Polymarket
   V2 effective dates from secondary sources.
6. No queue position anywhere: maker-side numbers remain upper bounds.
