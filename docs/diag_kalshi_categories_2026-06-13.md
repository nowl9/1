# Kalshi category probe -- maker-taker gap + settlement calibration (2026-06-13)

> Trade-tape maker returns are INCUMBENT realized returns -- they include
> the incumbents' queue position, selection, and skill, and do not promise
> our capture. Category census evidence, not edge evidence. Gross of
> exchange fees except where modeled. Nothing here answers fill survival
> (Q1) or authorizes a build -- the category-expansion row stays
> Stage-4-gated.

Sandboxed, read-only probe of Kalshi's PROD trade tape
(`api.elections.kalshi.com`) testing the external scalping doc's flagship
**maker-gap** claim (todo.md EXTERNAL-DOC TRIAGE; the
`docs/diag_bias_footprint_2026-06-11.md` row that was UNTESTED -- "no
sports data banked") and the **YES-premium / calibration** question on the
executable venue, versus the Polymarket finding in
`docs/diag_pm_history_2026-06-13.md` (-3.1pp pooled, MARKET-WRONG).
Zero repo/src changes. GET-only endpoints (markets / series / trades), no
order/portfolio calls, via the existing `_kalshi_auth.py` RSA-PSS signer.
All scripts + raw artifacts live under gitignored `outputs/kalshi_cat/`.
Recorder confirmed NOT running before the probe; probe completed and
stopped before any capture (Kalshi-REST-heavy; never run alongside one).

## TL;DR -- pre-registered verdicts

- **P1 maker gap: PARTIALLY TRANSFERS.** Doc prior was +2.23..+4.79pp in
  Sports/Ent vs +0.17pp Finance. Measured (per-trade, market-cluster
  bootstrap 95% CI, gross of fees):
  **ENT +2.26pp [+0.73, +3.71]** (lands at the bottom of the doc's range,
  CI excludes 0); **SPORTS -0.35pp [-3.06, +2.27]** (the doc's positive
  gap does NOT reproduce -- a fragile wash of large +/- series);
  **FINANCE +0.54pp [-0.09, +1.17]** (thin and NOT significant -- matches
  the doc's ~+0.17pp Finance-control prediction). The doc's "Sports/Ent"
  lumping is wrong: the gap is **Entertainment, not Sports**.
- **Even where it reproduces, it is not "maker alpha."** Kalshi's penny
  complement means `yes_price + no_price == 1.0000` on every trade, so
  maker excess is the EXACT negative of gross taker excess: **ENT +2.26pp
  == ENT takers lose 2.26pp gross**. It is a taker-overpay / market-
  miscalibration structure, amplified by takers leaning YES into markets
  that mostly resolve NO -- NOT evidence of category-specific maker skill,
  and smaller still net of fees.
- **The doc's "longshot-concentrated" attribution is REFUTED (direction
  wrong).** Longshot YES is universally OVERPRICED across all three
  categories (the taker-side optimism tax), but the maker's profit sits in
  the **favorite zone the maker holds** (ENT 0.50-0.95 by maker price =
  +9.36pp), with LOSSES in 0.05-0.50. The maker earns by holding cheap
  favorites against takers chasing overpriced longshots, not by harvesting
  sub-5c lottery tickets.
- **P2 vs Polymarket: -3.1pp does NOT transfer to the executable venue,
  and the apparent sign-flip is a framing artifact.** Kalshi BTC at PM's
  own 0.60-0.80 band reads +11.6pp (opposite sign), but that is the RIGHT
  ARM of a symmetric under-confidence S-curve (mirror: [0.20,0.50) =
  -10.1pp vs [0.50,0.80) = +10.6pp) that washes to an all-price
  **-0.83pp [-8.85, +6.87] (calibrated)**. PM's -3.1pp is a DIRECTIONAL
  favorite-overpricing; Kalshi BTC threshold is ~calibrated overall with
  symmetric price-shrinkage. The two are not comparable as a directional
  edge.
- **Net:** category-census evidence only. The one durable signal (ENT
  taker-loss / universal longshot overpricing) is real but fragile to a
  few mega-series, is a taker-miscalibration not demonstrated maker
  capture, is gross of fees, and is conditional on the sampled window.
  **The category-expansion row stays Stage-4-gated; nothing here
  authorizes a build.**

## 0. Method, provenance, honesty notes

- **Endpoints (read-only GET):** `/exchange/status`, `/series?category=`,
  `/markets?series_ticker=&status=settled&min_close_ts&max_close_ts`,
  `/markets/trades?ticker=`. No order/portfolio/fill endpoints touched.
  Auth via `btc_pm_arb.feeds._kalshi_auth` (RSA-PSS-SHA256), PROD forced
  (`kalshi_prod_url`) regardless of `KALSHI_USE_DEMO` -- demo has no
  settlement history.
- **Rate discipline:** sequential pulls, ~3-5 GET/s self-throttle,
  exponential backoff on 429. Every observed 429 recovered on the FIRST
  backoff cycle; the ">2 cycles on one endpoint -> STOP" tripwire never
  fired. Total ~1,589 trade GETs; `truncated=false`.
- **Enumeration fix (honest):** the global `/markets?status=settled` feed
  is ordered newest-first and the recent-days crypto/sports volume is so
  large that a full 90d sweep is intractable (it starved ENT, surfacing
  only its last ~2 days). Switched to **per-series windowed pulls**
  (`series_ticker` + `min/max_close_ts`), which bound each series and span
  the full 90 days. Per-series banking capped at 60 markets to span MANY
  series (a single SPORTS series, KXNBASPREAD, otherwise supplied 2,818 of
  the first 3,130 -> "Sports" would have meant "NBA spreads").
- **Maker excess formula (exact).** A trade has a taker and a maker on
  opposite outcomes; the maker holds the side the taker did NOT take:
  - `taker_side=='yes'` -> maker holds NO at `no_price`; settles $1 iff
    `result=='no'`.
  - `taker_side=='no'`  -> maker holds YES at `yes_price`; settles $1 iff
    `result=='yes'`.
  - `maker_excess = maker_settlement_value - maker_price` (dollars; x100 =
    pp). The "sign adjustment" of the external study IS this side
    selection. Gross of exchange fees.
- **Calibration:** each trade carries a YES price (= implied P(YES)) and
  the market's binary outcome (1 iff `result=='yes'`). Curve = realized
  P(YES) per YES-price bucket vs bucket mean price; `miscal = realized -
  price` (pp); negative == YES overpriced (the MARKET-WRONG framing).
- **CIs are market-cluster bootstraps** (resample markets, not trades),
  B=4000 -- outcomes are shared within a market so trades are not
  independent (naive trade-level CIs are ~6.8x too narrow on ENT). See the
  calibration clustering caveat in section 4.
- **Adversarial verification (4 subagents, gate-on-surprise).** Each
  independently re-derived one pillar from the raw tape (not importing the
  analysis code). All four reproduced the headline numbers to <0.01pp and
  confirmed no formula/sign/coding bug (0/375 trade-vs-market result
  mismatches). They also surfaced the load-bearing caveats folded in
  below: the maker==-taker mechanical identity, ENT significance
  fragility, the FINANCE symmetric-S-curve artifact, and the SPORTS
  calibration cluster-unit problem. A synthetic self-test
  (`_selftest_analyze.py`) independently checks the excess/outcome/Wilson/
  bucket math.

## 1. Sample design and actual n achieved

Window 2026-03-15 .. 2026-06-13 (90 days). Category groups mapped to
Kalshi's `category` field: SPORTS = `Sports`; ENT = `Entertainment`
(Kalshi has no separate "Culture"); FINANCE control = `Crypto` (the BTC
home) + `Financials` + `Economics`. Sampled up to 400 settled binary
markets per group (`market_type==binary`, `result in {yes,no}`,
`volume>0`), capped at 35 markets/series for breadth; FINANCE samples BTC
series first.

| Group | traded-binary pool | series in pool | markets w/ trades | trades | contracts | BTC markets |
|---|---|---|---|---|---|---|
| SPORTS  | 1,523 | 42 | 375 | 220,145 | 28.3M | 0 |
| ENT     | 1,529 | 72 | 395 |  50,993 |  6.5M | 0 |
| FINANCE | 1,557 | 88 | 394 | 327,493 | 31.6M | 154 |

All groups exceed the 200-500 target. FINANCE is ~90% BTC by trade count
(294,605 / 327,493), dominated by the ultra-liquid `KXBTC15M` intraday
directional (210k trades); the executable monthly/min/max barriers
(`KXBTCMAXMON`, `KXBTCMINMON`, ...) are present but thin. 0 block trades in
any group.

## 2. P1 -- maker-taker gap by category

Per-trade and volume-weighted maker excess, market-cluster bootstrap 95%
CI, gross of fees:

| Group | per-trade pp [95% CI] | vol-wt pp [95% CI] | doc prior |
|---|---|---|---|
| SPORTS  | **-0.35** [-3.06, +2.27] | -0.24 [-2.47, +1.88] | +2.23..+4.79 |
| ENT     | **+2.26** [+0.73, +3.71] | +1.87 [+1.04, +2.87] | +2.23..+4.79 |
| FINANCE | **+0.54** [-0.09, +1.17] | +0.62 [-0.08, +1.32] | +0.17 |

**Reproduces / partially transfers / fails:** PARTIALLY TRANSFERS.
- **ENT reproduces** (+2.26pp, CI excludes 0, lands at the low end of the
  doc's range). Broad: 45/58 series (78%) and 267/395 markets (68%) are
  positive. BUT (a) it is mechanically `-(`gross taker loss`)` (takers lose
  2.26pp gross, before fees), driven by takers leaning YES into a
  64%-resolve-NO slate -- a miscalibration structure, not maker skill; and
  (b) **significance is fragile**: excluding the top-3 trade-count series
  (KXLIUSAELIMINATION, KXBBCHARTDRAKE, KXFINALSATTEND) moves it to
  **+1.50pp [-0.05, +3.10]** (CI touches zero). The sign is broad; the
  >0 significance leans on a few high-churn franchises.
- **SPORTS fails** (-0.35pp, CI spans 0). It is a fragile WASH of large
  opposing series, not a structural zero: per-series per-trade excess
  spans -38.8pp to +35.2pp; dropping the single biggest contributor moves
  the aggregate to -1.18pp, dropping the top-3 to ~-0.02pp. No consistent
  maker gap in Sports.
- **FINANCE matches the doc's thin-Finance control** (+0.54pp ~ +0.17pp)
  but is NOT statistically distinguishable from 0; every decomposition
  (excl-KXBTC15M +0.67, KXBTC15M-only +0.47) also straddles 0.

**Price-bucket profile (longshot test).** Doc claims the gap is
longshot-concentrated. By YES-price bucket the maker excess is broadly
positive in ENT (peaking ~+7pp at 0.20-0.30) and sign-mixed in SPORTS/
FINANCE. Decomposed by the price the MAKER actually holds, the ENT profit
is concentrated in the **favorite zone (0.50-0.95 = +9.36pp)** with LOSSES
in 0.05-0.50 -- i.e. takers OVERPAY for longshots (universal, see section
3) and the maker profits by holding the complementary cheap favorite.
**The "maker gap lives in longshots" framing is refuted in direction;**
the longshot-OVERPRICING prior (taker optimism tax) is confirmed.

## 3. P2 -- settlement calibration

Realized P(YES) vs YES-price bucket (trade-weighted; `miscal = realized -
price`, pp; negative = YES overpriced). Wilson CIs are naive
(trade-level, too narrow -- see section 4); read the market/event-cluster
bootstrap for the 0.50-0.80 zone instead.

**SPORTS** -- classic favorite-longshot S-curve:

| bucket | n_tr | price | realized | miscal |
|---|---|---|---|---|
| [0.05,0.10) | 16,196 | 0.069 | 0.021 | -4.71 |
| [0.10,0.20) | 25,020 | 0.142 | 0.071 | -7.06 |
| [0.40,0.50) | 25,224 | 0.449 | 0.295 | -15.46 |
| [0.50,0.60) | 21,643 | 0.537 | 0.329 | -20.86 |
| [0.60,0.70) | 15,845 | 0.642 | 0.463 | -17.90 |
| [0.70,0.80) | 10,791 | 0.742 | 0.721 | -2.15 |
| [0.90,0.95) |  6,835 | 0.920 | 0.971 | +5.10 |

**ENT** -- strong longshot/mid overpricing (thin in the mid):

| bucket | n_tr | price | realized | miscal |
|---|---|---|---|---|
| [0.10,0.20) | 6,780 | 0.149 | 0.042 | -10.72 |
| [0.20,0.30) | 4,371 | 0.236 | 0.052 | -18.36 |
| [0.40,0.50) | 1,631 | 0.443 | 0.299 | -14.34 |
| [0.50,0.60) | 1,596 | 0.547 | 0.396 | -15.13 |
| [0.90,0.95) | 4,819 | 0.917 | 0.973 | +5.61 |

**FINANCE (BTC-dominated)** -- longshot overpricing too, but mid-high YES
UNDERpriced (the right arm of the S-curve):

| bucket | n_tr | price | realized | miscal |
|---|---|---|---|---|
| [0.05,0.10) | 31,429 | 0.070 | 0.002 | -6.86 |
| [0.10,0.20) | 38,596 | 0.143 | 0.034 | -10.87 |
| [0.20,0.30) | 32,118 | 0.242 | 0.138 | -10.32 |
| [0.50,0.60) | 21,897 | 0.543 | 0.617 | +7.43 |
| [0.60,0.70) | 26,304 | 0.644 | 0.759 | +11.44 |
| [0.70,0.80) | 22,463 | 0.743 | 0.851 | +10.82 |
| [0.80,0.90) | 27,109 | 0.848 | 0.949 | +10.13 |

**The 0.50-0.80 zone vs the Polymarket -3.1pp finding** (market-cluster
bootstrap; SPORTS also re-clustered at the game-event level since strikes
within one game share an outcome):

| Group | miscal 0.50-0.80 | 95% CI (market-cluster) | note |
|---|---|---|---|
| SPORTS  | -15.70pp | [-28.70, +0.08] (strike) / **[-32.55, +11.19] (game)** | crosses 0 at event level |
| ENT     |  -6.00pp | [-31.47, +15.24] | thin, NS |
| FINANCE | +10.00pp | [-5.18, +23.21] | S-curve right arm, NS |

Findings, honestly:
- **Universal longshot YES-overpricing** (0.05-0.30 settles well below
  price in all three groups, -4 to -18pp) is the robust, broad result --
  the taker-side "optimism tax." This is where the ENT maker gap comes
  from.
- **SPORTS 0.50-0.80 (-15.70pp)** is directionally MARKET-WRONG (YES /
  favorites overpriced, agreeing with PM's sign) but **fragile and
  concentrated**: it lives in the 0.50-0.60 bin (-20.9pp) and in a few
  NBA/soccer over-under strike ladders (e.g. KXNBASPREAD: 0/10 zone
  markets resolved YES, thousands of trades each); the true-favorite
  0.70-0.80 band is essentially calibrated (-2.15pp). Re-clustering at the
  game-event level (133 vs 183 clusters) widens the CI to cross zero. It
  is a strike-ladder selection effect, not a clean ~5x-PM favorite bias.
- **FINANCE/BTC +10pp is an artifact, not a directional edge.** It is the
  right arm of a near-perfectly symmetric under-confidence S-curve:
  BTC-threshold [0.20,0.50) = -10.12pp vs [0.50,0.80) = +10.60pp, washing
  to an all-price **-0.83pp [-8.85, +6.87] (calibrated)**. Prices are
  shrunk toward 0.50 (under-confident), not directionally biased.
  "Above-strike" is a non-distinction here (71/74 BTC threshold markets
  are above-type; 3 below trades), and there is no up-trend window driving
  it (0.50-0.80 markets settle YES ~58%, slightly DECLINING late). So
  **the Polymarket -3.1pp does not transfer to the executable venue**;
  comparing PM's directional -3.1pp to Kalshi's +11.6pp at 0.60-0.80 is a
  framing error (Kalshi's is symmetric S-curve shrinkage).

## 4. Product-class separation (FINANCE barrier-heavy caveat)

The FINANCE/BTC group mixes threshold binaries with path/barrier products
(one-touch min/max), whose calibration is NOT directly comparable to
threshold binaries. Separated (maker excess per-trade; 0.50-0.80 miscal;
market-cluster CI):

| class | n_mkts | n_tr | maker excess pp [CI] | 0.50-0.80 miscal [CI] |
|---|---|---|---|---|
| threshold     | 284 | 252,785 | +0.40 [-0.33, +1.07] | +9.26 [-8.30, +23.40] |
| barrier (min/max touch) | 48 | 62,957 | +1.39 [-0.00, +2.57] | +11.33 [-21.07, +37.87] |
| event-binary  |  62 |  11,751 | -0.97 [-7.65, +1.31] | +19.50 [-43.96, +24.83] |
| BTC-only (any class) | 154 | 294,605 | +0.51 [-0.15, +1.14] | +10.76 [-5.00, +23.91] |

Every class's 0.50-0.80 miscal CI spans zero; the barrier (one-touch)
calibration in particular is path-dependent and should not be read as a
threshold-binary calibration number. No class shows a maker-excess gap
that clears zero.

## 5. Limitations / what this is NOT

- **Maker == -taker (mechanical).** `yes+no==1.0000` exactly, so every
  "maker gap" is identically a gross taker loss. It measures market
  miscalibration / effective spread paid by takers, not maker skill, and
  is gross of fees (memory: PM crypto taker 0.07, KXBTC* maker fee zero --
  a sub-1pp gross gap is plausibly neutral-to-negative net).
- **Incumbent returns.** Realized returns embed the incumbents' queue
  position, selection, and skill; they do not promise our capture (Q1 /
  fill survival is untouched here).
- **Calibration CIs are clustered on market (ticker).** For strike-ladder
  products (sports over/under, BTC threshold ladders) the correct unit is
  the game/event; event-level clustering widens CIs (SPORTS 0.50-0.80
  crosses zero). Read the calibration "significance" at the event level:
  mostly NOT significant.
- **Page-cap truncation.** Trade tapes capped at 6,000 trades/market
  (newest-first): 7 SPORTS / 2 ENT / 38 FINANCE markets hit the cap, so
  per-trade means for those are conditioned on near-settlement activity.
  Verified this does not flip any sign, but it limits external validity of
  absolute per-trade FINANCE statistics.
- **Window-conditional.** Estimates are conditional on the 2026-03-15 ..
  06-13 realized outcome path (ENT especially: 64% of markets resolved
  NO). Not guaranteed to generalize.
- **Sampling.** Traded settled binaries only; 35-markets/series cap for
  breadth; trade-weighted means over-represent high-churn series.

## 6. Verdict and disposition

- The external doc's flagship Sports/Ent maker gap **partially transfers**:
  confirmed in **Entertainment** (+2.26pp, fragile, taker-loss not maker
  skill), **absent in Sports** (a wash), **thin in Finance** (as the doc
  itself predicted). The "longshot-concentrated" attribution is **refuted
  in direction** -- longshots are overpriced (taker tax); the maker profit
  is in the complementary favorite zone.
- The Polymarket mid-life YES-overpricing (-3.1pp) **does not transfer to
  Kalshi's executable venue**; Kalshi BTC threshold is ~calibrated
  all-price with symmetric under-confidence, and the apparent sign-flip at
  0.60-0.80 is a measurement-framing artifact.
- This is **category census evidence, not edge evidence.** It abandons the
  options/SVI anchor (no fair exists for sports/ent) and would be a
  different, book/flow-anchored epistemic foundation -- a new row, not an
  extension of the LEAD. **The category-expansion row stays Stage-4-gated;
  nothing here authorizes a build.** (Updates the
  `diag_bias_footprint_2026-06-11.md` "Sports/Ent category expansion" row
  from UNTESTED to TESTED -- partially transfers, longshot framing
  refuted.)

Artifacts (gitignored): `outputs/kalshi_cat/` -- `_kc_common.py` (signed
GET + backoff), `pull_enumerate2.py` (per-series enumeration),
`pull_trades.py` (trade-tape pull), `analyze.py` / `analyze_robust.py` /
`analyze_addendum.py` (P1/P2 + decomposition + event-clustering),
`_selftest_analyze.py`, and `data/` (markets_pool, trades_*, manifests,
analysis_results.json).
