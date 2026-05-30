"""Data normalizer — converts raw feed data into canonical ProbabilityQuote.

For the Deribit options feed, converting a raw OptionTick to a
ProbabilityQuote requires a vol surface (Layer 2).  This module only handles
the identity-level normalization: timestamps, probability bounds from
prediction markets, and lightweight pass-through from the options feed.

Full options→probability conversion is delegated to pricing.digital_pricer.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from btc_pm_arb.models import DataSource, OptionTick, PredictionMarketTick, ProbabilityQuote


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ── Polarity / product-type classification ────────────────────────────────────
#
# A Kalshi YES leg can pay on "above" or "below" the strike, and the contract
# can be a terminal European digital (settles on the price AT expiry) or a
# path-dependent one-touch barrier (settles if the price is EVER above/below
# the strike through the period).  The pricer only computes a terminal
# P(S_T > K); mislabelling a "below" leg as "above", or a one-touch barrier as
# a terminal digital, produces phantom edges.  We classify both here, at the
# single shared normalization boundary, from the raw venue metadata when it is
# present and from the ticker series as a robust fallback when it is not (e.g.
# replayed price-only frames carry the ticker but no metadata).

def classify_kalshi_direction(raw: dict, ticker: str = "") -> str:
    """Return the YES-leg polarity ("above" | "below") for a Kalshi market.

    Preference order: explicit ``strike_type`` -> ``yes_sub_title`` text ->
    ticker series pattern -> default "above".
    """
    strike_type = str(raw.get("strike_type") or "").strip().lower()
    if strike_type in ("greater", "greater_or_equal", "above"):
        return "above"
    if strike_type in ("less", "less_or_equal", "below"):
        return "below"

    sub = str(raw.get("yes_sub_title") or "").strip().lower()
    if any(w in sub for w in ("below", "under", "less than", "at most", "<=", "<")):
        return "below"
    if any(w in sub for w in ("above", "over", "greater", "at least", ">=", ">")):
        return "above"

    t = (ticker or "").upper()
    if "MINMON" in t:   # one-touch *minimum* barrier: YES if EVER below
        return "below"
    if "MAXMON" in t:   # one-touch *maximum* barrier: YES if EVER above
        return "above"
    return "above"


def classify_kalshi_product_type(raw: dict, ticker: str = "") -> str:
    """Return "one_touch" for path-dependent barriers, else "terminal".

    A market is a one-touch barrier when its rules say the level is "ever"
    breached, OR it exposes an early-close / settlement-timer touch mechanism,
    OR it belongs to a known barrier series (KXBTCMINMON / KXBTCMAXMON).
    Genuine terminal series (e.g. KXBTCMAX150, KXBTCD) stay "terminal".
    """
    rules = str(raw.get("rules_primary") or "").lower()
    if "ever" in rules:
        return "one_touch"
    if raw.get("early_close_condition") and raw.get("settlement_timer_seconds"):
        return "one_touch"

    t = (ticker or "").upper()
    if "MINMON" in t or "MAXMON" in t:
        return "one_touch"
    return "terminal"


# ── Polymarket hard denylist (stopgap, independent of the classifier) ──────────
#
# Polymarket questions are free text with no strike_type / rules metadata, so the
# structured Kalshi parse does not apply.  Before the free-text classifier exists
# this denylist hard-excludes the specific barrier-class + range BTC markets
# enumerated in the polarity audit (outputs/fix_polarity_barrier_report.md
# section 4), fencing the live "dip to" one-touch-down phantoms.
#
# It is matched against the two identifiers a PredictionMarketTick actually
# carries at runtime: the venue ``condition_id`` (stored as ``contract_id``) and
# the normalized free-text ``question``.  The live Gamma recordings that carry
# conditionIds (data/recordings/) are gitignored and absent from the checkout, so
# the audit's enumerated *question text* is the stable denylist key; the
# condition_id set is kept as a hook for when those recordings are recaptured.
#
# A denylisted market is forced off the terminal pricer (see
# normalize_polymarket_tick): it is tracked but never produces a signal.


def _normalize_question(question: str) -> str:
    """Collapse whitespace + lowercase a free-text question for stable matching."""
    return " ".join((question or "").split()).strip().lower()


# Venue condition_ids / token_ids to hard-exclude.  Empty until the gitignored
# Gamma recordings are recaptured; the question denylist below carries the
# audit's enumerated cases in the meantime.
_PM_DENYLIST_CONDITION_IDS: frozenset[str] = frozenset()

# Enumerated barrier-class + range BTC markets from the polarity audit.  The
# genuine terminal-below ("be less than $X on [date]") and terminal-above markets
# are intentionally NOT denylisted -- they are retained and priced.
_PM_DENYLIST_QUESTIONS: frozenset[str] = frozenset(
    _normalize_question(q)
    for q in (
        # one-touch DOWN barrier ("dip to ...") -- the LIVE phantom class
        "Will Bitcoin dip to $70,000 May 25-31?",
        # one-touch UP barrier ("reach ... by [date]") -- latent
        "Will Bitcoin reach $150,000 by December 31, 2026?",
        # range / band product ("between $X and $Y on [date]") -- unpriceable
        "Will the price of Bitcoin be between $64,000 and $66,000 on June 4?",
    )
)


def is_polymarket_denylisted(contract_id: str, question: str) -> bool:
    """True if a Polymarket market is on the hard denylist (track-but-don't-signal).

    Independent of the free-text classifier: matches the venue condition_id or
    the normalized question text against the audited barrier/range set.
    """
    if contract_id and contract_id in _PM_DENYLIST_CONDITION_IDS:
        return True
    return _normalize_question(question) in _PM_DENYLIST_QUESTIONS


# ── Polymarket free-text question classifier ───────────────────────────────────
#
# Polymarket markets carry only a free-text ``question`` -- no strike_type /
# yes_sub_title / rules_primary -- so the structured Kalshi parse does not apply.
# This deterministic keyword classifier is the Polymarket analog of
# classify_kalshi_direction / classify_kalshi_product_type: it maps the question
# phrasing to (direction, product_type) so barriers and bands are excluded and
# genuine terminals are correctly polarized.
#
# FAIL CLOSED.  The original defect was fail-OPEN: every PM contract defaulted to
# direction=above / product_type=terminal, turning "dip to" one-touch-downs into
# live phantoms.  Here the default for any ambiguous / unmatched / two-sided
# question is EXCLUDE (product_type=one_touch), never terminal-above.  An idle
# missed market is acceptable; a phantom is not.
#
# Vocabulary (from outputs/fix_polarity_barrier_report.md section 4):
#   reach / hit / touch / dip-to / fall-to / drop-to  -> one_touch (barrier)
#   between X and Y / range                            -> range (band)
#   above / over / exceed ... on|by [date]            -> terminal, above
#   below / under / less than ... on|by [date]         -> terminal, below

_PM_RANGE = re.compile(r"\bbetween\b.+?\band\b|\brange\b", re.I)
_PM_ONE_TOUCH = re.compile(
    r"\b(reach(?:es|ed)?|hits?|hitting|touch(?:es|ed)?|dips?\s+to|falls?\s+to|"
    r"drops?\s+to|sinks?\s+to|climbs?\s+to|rises?\s+to|gets?\s+to|ever|"
    r"all[-\s]?time\s+high|record\s+high)\b",
    re.I,
)
_PM_DOWN_TOUCH = re.compile(
    r"\b(dips?\s+to|falls?\s+to|drops?\s+to|sinks?\s+to|declines?\s+to)\b", re.I
)
_PM_BELOW = re.compile(
    r"\b(below|under|less\s+than|lower\s+than|beneath|at\s+most)\b", re.I
)
_PM_ABOVE = re.compile(
    r"\b(above|over|greater\s+than|higher\s+than|more\s+than|exceeds?|"
    r"surpass(?:es)?|at\s+least)\b",
    re.I,
)
# A terminal directional market is anchored to a settlement date ("on"/"by").
_PM_DATED = re.compile(r"\b(on|by)\b", re.I)


def classify_polymarket_product_type(question: str) -> str:
    """Return "terminal" | "one_touch" | "range" for a Polymarket question.

    FAIL CLOSED: anything that is not unambiguously a single-threshold terminal
    market anchored to a date resolves to "one_touch" (excluded), never
    "terminal".
    """
    q = question or ""
    if _PM_RANGE.search(q):
        return "range"
    if _PM_ONE_TOUCH.search(q):
        return "one_touch"
    has_below = bool(_PM_BELOW.search(q))
    has_above = bool(_PM_ABOVE.search(q))
    # Terminal only when there is exactly one comparator direction AND a
    # settlement-date anchor; otherwise exclude.
    if _PM_DATED.search(q) and (has_below ^ has_above):
        return "terminal"
    return "one_touch"


def classify_polymarket_direction(question: str) -> str:
    """Return the YES-leg polarity ("above" | "below") for a Polymarket question.

    "dip to" / "fall to" / "drop to" are down-touches (below); an explicit
    below comparator with no above comparator is below; everything else
    (including the excluded default) is above.  Direction only affects pricing
    for *retained* terminals -- excluded barriers/bands never reach the pricer.
    """
    q = question or ""
    if _PM_DOWN_TOUCH.search(q):
        return "below"
    has_below = bool(_PM_BELOW.search(q))
    has_above = bool(_PM_ABOVE.search(q))
    if has_below and not has_above:
        return "below"
    return "above"


# ── Prediction market normalizers ─────────────────────────────────────────────

def normalize_polymarket_tick(raw: dict) -> PredictionMarketTick:
    """Normalize a raw Polymarket CLOB order-book snapshot.

    Expected raw keys (from py-clob-client market snapshot):
      token_id, question, outcomes, outcomePrices, volume, endDate, ...

    YES is always the first outcome element (index 0) by convention.
    """
    outcomes: list[str] = raw.get("outcomes", ["Yes", "No"])
    prices: list[str] = raw.get("outcomePrices", ["0.5", "0.5"])

    try:
        yes_idx = next(
            i for i, o in enumerate(outcomes) if o.lower() in ("yes", "1")
        )
    except StopIteration:
        yes_idx = 0

    try:
        no_idx = next(
            i for i, o in enumerate(outcomes) if o.lower() in ("no", "0")
        )
    except StopIteration:
        no_idx = 1 if yes_idx == 0 else 0

    def _price(idx: int) -> float | None:
        try:
            return float(prices[idx])
        except (IndexError, ValueError):
            return None

    yes_mid = _price(yes_idx)
    no_mid = _price(no_idx)

    # Polymarket order book bid/ask (may be present in detailed snapshots)
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None

    if "bids" in raw and "asks" in raw:
        bids = raw["bids"]
        asks = raw["asks"]
        if bids:
            yes_bid = float(bids[0]["price"])
        if asks:
            yes_ask = float(asks[0]["price"])
        if yes_bid is not None and yes_ask is not None:
            no_bid = round(1 - yes_ask, 6)
            no_ask = round(1 - yes_bid, 6)
    elif yes_mid is not None:
        # Only mid available
        yes_bid = yes_ask = yes_mid
        no_bid = no_ask = no_mid

    end_date: str | None = raw.get("endDate") or raw.get("end_date_iso")
    expiry: datetime | None = None
    if end_date:
        try:
            expiry = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            expiry = None

    question = raw.get("question", "")
    # Try to extract a BTC strike price from the question text
    strike = _extract_strike_from_question(question)
    contract_id = raw.get("condition_id") or raw.get("token_id", "")

    # Polarity / product type parsed from the free-text question (fail-closed:
    # ambiguous/unmatched -> one_touch, never terminal-above).  The hard denylist
    # then force-excludes audited barrier/range markets even if the classifier
    # would have passed them; the ``terminal``-only guard keeps the denylist
    # independent of (and additive to) the classifier -- a denylisted market is
    # never left on the terminal pricer, and a market the classifier already
    # excluded keeps its more precise one_touch/range tag.
    direction = classify_polymarket_direction(question)
    product_type = classify_polymarket_product_type(question)
    if is_polymarket_denylisted(contract_id, question) and product_type == "terminal":
        product_type = "one_touch"

    return PredictionMarketTick(
        source=DataSource.POLYMARKET,
        contract_id=contract_id,
        question=question,
        strike=strike,
        expiry=expiry,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=utc_now(),
        direction=direction,
        product_type=product_type,
    )


def normalize_kalshi_tick(raw: dict) -> PredictionMarketTick:
    """Normalize a raw Kalshi market snapshot.

    Post March-2026 migration, Kalshi prices are fixed-point dollar
    strings in [0, 1] (e.g. ``"0.6200"`` = 62¢).  ``_build_tick``
    pre-aggregates these into floats; this function accepts either
    floats or string-encoded floats via the ``_dollar_to_prob`` helper.
    """
    def _dollar_to_prob(v: object) -> float | None:
        if v is None:
            return None
        try:
            return round(float(v), 4)
        except (ValueError, TypeError):
            return None

    yes_bid = _dollar_to_prob(raw.get("yes_bid_dollars"))
    yes_ask = _dollar_to_prob(raw.get("yes_ask_dollars"))
    no_bid = _dollar_to_prob(raw.get("no_bid_dollars"))
    no_ask = _dollar_to_prob(raw.get("no_ask_dollars"))

    # If only last_price available use it as mid
    if yes_bid is None and yes_ask is None:
        last = raw.get("last_price_dollars")
        if last is not None:
            mid = _dollar_to_prob(last)
            yes_bid = yes_ask = mid
            no_bid = no_ask = round(1 - mid, 4) if mid is not None else None

    close_time: str | None = raw.get("close_time") or raw.get("expiration_time")
    expiry: datetime | None = None
    if close_time:
        try:
            expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            expiry = None

    subtitle: str = raw.get("subtitle", "") or raw.get("title", "")
    strike = _extract_strike_from_question(subtitle)

    ticker = raw.get("ticker", raw.get("market_id", ""))
    direction = classify_kalshi_direction(raw, ticker)
    product_type = classify_kalshi_product_type(raw, ticker)

    return PredictionMarketTick(
        source=DataSource.KALSHI,
        contract_id=ticker,
        question=raw.get("title", subtitle),
        strike=strike,
        expiry=expiry,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=utc_now(),
        order_book_yes=list(raw.get("order_book_yes") or []),
        order_book_no=list(raw.get("order_book_no") or []),
        direction=direction,
        product_type=product_type,
    )


def pm_tick_to_probability_quote(tick: PredictionMarketTick) -> ProbabilityQuote | None:
    """Convert a PredictionMarketTick to a ProbabilityQuote.

    Returns None if essential fields (strike, expiry, prices) are missing.
    """
    if tick.strike is None or tick.expiry is None:
        return None
    if tick.yes_bid is None or tick.yes_ask is None:
        return None

    mid = tick.yes_mid
    if mid is None:
        return None

    settlement_type: str
    if tick.source == DataSource.KALSHI:
        settlement_type = "kalshi_rti"
    elif tick.source == DataSource.POLYMARKET:
        settlement_type = "polymarket_spot"
    else:
        settlement_type = "unknown"

    return ProbabilityQuote(
        source=tick.source,
        contract_id=tick.contract_id,
        strike=tick.strike,
        expiry=tick.expiry,
        bid_prob=tick.yes_bid,
        ask_prob=tick.yes_ask,
        mid_prob=mid,
        direction=tick.direction,
        settlement_type=settlement_type,  # type: ignore[arg-type]
        timestamp=tick.timestamp,
        product_type=tick.product_type,
        order_book_yes=tick.order_book_yes,
        order_book_no=tick.order_book_no,
    )


# ── Strike extraction ─────────────────────────────────────────────────────────

import re as _re

# Matches patterns like "$100,000", "$100k", "100000", "100K" inside questions
_STRIKE_PATTERNS = [
    _re.compile(r"\$([0-9]{1,3}(?:,[0-9]{3})+)"),   # $100,000
    _re.compile(r"\$([0-9]+[kK])"),                   # $100k / $100K
    _re.compile(r"\b([0-9]{5,6})\b"),                 # bare 5-6 digit integer
]


def _extract_strike_from_question(question: str) -> float | None:
    """Best-effort parse of a USD threshold from a free-text question."""
    for pattern in _STRIKE_PATTERNS:
        m = pattern.search(question)
        if m:
            raw = m.group(1).replace(",", "")
            if raw.lower().endswith("k"):
                try:
                    return float(raw[:-1]) * 1000
                except ValueError:
                    continue
            try:
                return float(raw)
            except ValueError:
                continue
    return None
