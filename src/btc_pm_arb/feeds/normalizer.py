"""Data normalizer — converts raw feed data into canonical ProbabilityQuote.

For the Deribit options feed, converting a raw OptionTick to a
ProbabilityQuote requires a vol surface (Layer 2).  This module only handles
the identity-level normalization: timestamps, probability bounds from
prediction markets, and lightweight pass-through from the options feed.

Full options→probability conversion is delegated to pricing.digital_pricer.
"""

from __future__ import annotations

from datetime import datetime, timezone

from btc_pm_arb.models import DataSource, OptionTick, PredictionMarketTick, ProbabilityQuote


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


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

    # Try to extract a BTC strike price from the question text
    strike = _extract_strike_from_question(raw.get("question", ""))

    return PredictionMarketTick(
        source=DataSource.POLYMARKET,
        contract_id=raw.get("condition_id") or raw.get("token_id", ""),
        question=raw.get("question", ""),
        strike=strike,
        expiry=expiry,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=utc_now(),
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

    return PredictionMarketTick(
        source=DataSource.KALSHI,
        contract_id=raw.get("ticker", raw.get("market_id", "")),
        question=raw.get("title", subtitle),
        strike=strike,
        expiry=expiry,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=utc_now(),
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
        direction="above",
        settlement_type=settlement_type,  # type: ignore[arg-type]
        timestamp=tick.timestamp,
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
