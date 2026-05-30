"""Tests for the feed normalizer (Polymarket + Kalshi)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from btc_pm_arb.feeds.normalizer import (
    _extract_strike_from_question,
    classify_kalshi_direction,
    classify_kalshi_product_type,
    classify_polymarket_direction,
    classify_polymarket_product_type,
    is_polymarket_denylisted,
    normalize_kalshi_tick,
    normalize_polymarket_tick,
    pm_tick_to_probability_quote,
)
from btc_pm_arb.models import DataSource


class TestExtractStrikeFromQuestion:
    def test_dollar_comma_format(self) -> None:
        assert _extract_strike_from_question("Will BTC be above $100,000?") == 100_000.0

    def test_dollar_k_lowercase(self) -> None:
        assert _extract_strike_from_question("BTC above $80k by June") == 80_000.0

    def test_dollar_k_uppercase(self) -> None:
        assert _extract_strike_from_question("Price above $100K") == 100_000.0

    def test_bare_integer(self) -> None:
        assert _extract_strike_from_question("Bitcoin hits 95000 target") == 95_000.0

    def test_no_match(self) -> None:
        assert _extract_strike_from_question("Will it rain tomorrow?") is None

    def test_prefers_dollar_comma_over_bare(self) -> None:
        # Both patterns match; first regex wins ($100,000)
        result = _extract_strike_from_question("BTC above $100,000 or 90000?")
        assert result == 100_000.0


class TestNormalizePolymarketTick:
    def _base_raw(self, **overrides: object) -> dict:
        raw = {
            "condition_id": "abc123",
            "question": "Will BTC be above $100,000 by June 30?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.62", "0.38"],
            "endDate": "2024-06-30T00:00:00Z",
        }
        raw.update(overrides)
        return raw

    def test_yes_mid_probability(self) -> None:
        tick = normalize_polymarket_tick(self._base_raw())
        assert tick.yes_mid == pytest.approx(0.62)

    def test_no_mid_probability(self) -> None:
        tick = normalize_polymarket_tick(self._base_raw())
        assert tick.no_mid == pytest.approx(0.38)

    def test_source_is_polymarket(self) -> None:
        tick = normalize_polymarket_tick(self._base_raw())
        assert tick.source == DataSource.POLYMARKET

    def test_contract_id_from_condition_id(self) -> None:
        tick = normalize_polymarket_tick(self._base_raw())
        assert tick.contract_id == "abc123"

    def test_strike_extracted(self) -> None:
        tick = normalize_polymarket_tick(self._base_raw())
        assert tick.strike == 100_000.0

    def test_expiry_parsed(self) -> None:
        tick = normalize_polymarket_tick(self._base_raw())
        assert tick.expiry is not None
        assert tick.expiry == datetime(2024, 6, 30, 0, 0, 0, tzinfo=timezone.utc)

    def test_with_bid_ask_in_raw(self) -> None:
        raw = self._base_raw(
            bids=[{"price": "0.60", "size": "100"}],
            asks=[{"price": "0.64", "size": "80"}],
        )
        tick = normalize_polymarket_tick(raw)
        assert tick.yes_bid == pytest.approx(0.60)
        assert tick.yes_ask == pytest.approx(0.64)
        # no_bid = 1 - yes_ask, no_ask = 1 - yes_bid
        assert tick.no_bid == pytest.approx(1 - 0.64, abs=1e-5)
        assert tick.no_ask == pytest.approx(1 - 0.60, abs=1e-5)

    def test_missing_end_date(self) -> None:
        raw = self._base_raw()
        del raw["endDate"]
        tick = normalize_polymarket_tick(raw)
        assert tick.expiry is None

    def test_yes_outcome_in_various_cases(self) -> None:
        raw = self._base_raw(outcomes=["YES", "NO"], outcomePrices=["0.7", "0.3"])
        tick = normalize_polymarket_tick(raw)
        assert tick.yes_mid == pytest.approx(0.7)


class TestNormalizeKalshiTick:
    def _base_raw(self, **overrides: object) -> dict:
        # Post March-2026 migration: dollar-string fields, not integer cents.
        raw = {
            "ticker": "KXBTC-24DEC31-B100000",
            "title": "Will BTC be above $100,000 on Dec 31?",
            "subtitle": "BTC above $100,000",
            "yes_bid_dollars": "0.62",
            "yes_ask_dollars": "0.65",
            "no_bid_dollars": "0.35",
            "no_ask_dollars": "0.38",
            "close_time": "2024-12-31T23:59:00Z",
        }
        raw.update(overrides)
        return raw

    def test_dollar_strings_parsed_to_float(self) -> None:
        tick = normalize_kalshi_tick(self._base_raw())
        assert tick.yes_bid == pytest.approx(0.62)
        assert tick.yes_ask == pytest.approx(0.65)
        assert tick.no_bid == pytest.approx(0.35)
        assert tick.no_ask == pytest.approx(0.38)

    def test_source_is_kalshi(self) -> None:
        tick = normalize_kalshi_tick(self._base_raw())
        assert tick.source == DataSource.KALSHI

    def test_contract_id_from_ticker(self) -> None:
        tick = normalize_kalshi_tick(self._base_raw())
        assert tick.contract_id == "KXBTC-24DEC31-B100000"

    def test_strike_extracted_from_subtitle(self) -> None:
        tick = normalize_kalshi_tick(self._base_raw())
        assert tick.strike == 100_000.0

    def test_expiry_parsed(self) -> None:
        tick = normalize_kalshi_tick(self._base_raw())
        assert tick.expiry is not None
        assert tick.expiry.year == 2024
        assert tick.expiry.month == 12
        assert tick.expiry.day == 31

    def test_fallback_to_last_price_when_no_bid_ask(self) -> None:
        raw = self._base_raw()
        del raw["yes_bid_dollars"]
        del raw["yes_ask_dollars"]
        del raw["no_bid_dollars"]
        del raw["no_ask_dollars"]
        raw["last_price_dollars"] = "0.55"
        tick = normalize_kalshi_tick(raw)
        assert tick.yes_bid == pytest.approx(0.55)
        assert tick.yes_ask == pytest.approx(0.55)

    def test_none_when_no_prices(self) -> None:
        raw = {"ticker": "KXBTC-X", "title": "BTC above $50,000?", "close_time": "2024-12-31T00:00:00Z"}
        tick = normalize_kalshi_tick(raw)
        assert tick.yes_bid is None
        assert tick.yes_ask is None


class TestPmTickToProbabilityQuote:
    def _kalshi_tick(self) -> object:
        # Post March-2026 migration: dollar-string fields, not integer cents.
        raw = {
            "ticker": "KXBTC-24DEC31-B100000",
            "title": "BTC above $100,000?",
            "subtitle": "BTC above $100,000",
            "yes_bid_dollars": "0.62",
            "yes_ask_dollars": "0.65",
            "no_bid_dollars": "0.35",
            "no_ask_dollars": "0.38",
            "close_time": "2024-12-31T23:59:00Z",
        }
        return normalize_kalshi_tick(raw)

    def test_probability_quote_from_kalshi(self) -> None:
        tick = self._kalshi_tick()
        quote = pm_tick_to_probability_quote(tick)
        assert quote is not None
        assert quote.source == DataSource.KALSHI
        assert quote.strike == 100_000.0
        assert quote.bid_prob == pytest.approx(0.62)
        assert quote.ask_prob == pytest.approx(0.65)
        assert quote.mid_prob == pytest.approx(0.635)
        assert quote.settlement_type == "kalshi_rti"

    def test_returns_none_when_strike_missing(self) -> None:
        from btc_pm_arb.models import PredictionMarketTick
        from datetime import datetime, timezone
        tick = PredictionMarketTick(
            source=DataSource.KALSHI,
            contract_id="x",
            question="unknown",
            strike=None,
            expiry=datetime(2025, 1, 1, tzinfo=timezone.utc),
            yes_bid=0.5,
            yes_ask=0.5,
            timestamp=datetime.now(timezone.utc),
        )
        assert pm_tick_to_probability_quote(tick) is None

    def test_returns_none_when_expiry_missing(self) -> None:
        from btc_pm_arb.models import PredictionMarketTick
        from datetime import datetime, timezone
        tick = PredictionMarketTick(
            source=DataSource.POLYMARKET,
            contract_id="x",
            question="BTC above $50,000?",
            strike=50_000.0,
            expiry=None,
            yes_bid=0.6,
            yes_ask=0.6,
            timestamp=datetime.now(timezone.utc),
        )
        assert pm_tick_to_probability_quote(tick) is None

    def test_polymarket_settlement_type(self) -> None:
        raw = {
            "condition_id": "abc",
            "question": "Will BTC be above $80,000 by June 30?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.55", "0.45"],
            "endDate": "2024-06-30T00:00:00Z",
        }
        tick = normalize_polymarket_tick(raw)
        quote = pm_tick_to_probability_quote(tick)
        assert quote is not None
        assert quote.settlement_type == "polymarket_spot"


class TestPolarityClassification:
    """Direction parse: strike_type / yes_sub_title / ticker-series fallback."""

    def test_strike_type_greater_is_above(self) -> None:
        assert classify_kalshi_direction({"strike_type": "greater"}, "") == "above"

    def test_strike_type_less_is_below(self) -> None:
        assert classify_kalshi_direction({"strike_type": "less"}, "") == "below"

    def test_yes_sub_title_below(self) -> None:
        assert classify_kalshi_direction({"yes_sub_title": "Below $70,000.00"}, "") == "below"

    def test_yes_sub_title_above(self) -> None:
        assert classify_kalshi_direction({"yes_sub_title": "Above $90,000.00"}, "") == "above"

    def test_minmon_ticker_fallback_below(self) -> None:
        assert classify_kalshi_direction({}, "KXBTCMINMON-BTC-26MAY31-7000000") == "below"

    def test_maxmon_ticker_fallback_above(self) -> None:
        assert classify_kalshi_direction({}, "KXBTCMAXMON-BTC-26MAY31-9000000") == "above"

    def test_default_is_above(self) -> None:
        assert classify_kalshi_direction({}, "KXBTC-24DEC31-B100000") == "above"


class TestProductTypeClassification:
    """Barrier (one-touch) vs terminal classification."""

    def test_rules_ever_is_one_touch(self) -> None:
        raw = {"rules_primary": "If BTC is ever below $70000.00, resolves Yes."}
        assert classify_kalshi_product_type(raw, "KXBTCD-X") == "one_touch"

    def test_early_close_plus_timer_is_one_touch(self) -> None:
        raw = {"early_close_condition": "touch", "settlement_timer_seconds": 1800}
        assert classify_kalshi_product_type(raw, "KXBTCD-X") == "one_touch"

    def test_minmon_ticker_is_one_touch(self) -> None:
        assert classify_kalshi_product_type({}, "KXBTCMINMON-BTC-26MAY31-7000000") == "one_touch"

    def test_maxmon_ticker_is_one_touch(self) -> None:
        assert classify_kalshi_product_type({}, "KXBTCMAXMON-BTC-26MAY31-9000000") == "one_touch"

    def test_max150_terminal_not_one_touch(self) -> None:
        raw = {
            "strike_type": "greater",
            "rules_primary": "If the price of Bitcoin is above 149999.99 by Dec 31 at 11:59PM ET",
        }
        assert classify_kalshi_product_type(raw, "KXBTCMAX150-26DEC31") == "terminal"

    def test_plain_terminal(self) -> None:
        assert classify_kalshi_product_type({}, "KXBTC-24DEC31-B100000") == "terminal"


class TestNormalizeKalshiPolarityProductType:
    """normalize_kalshi_tick carries direction + product_type onto the tick."""

    def _raw(self, **overrides: object) -> dict:
        raw = {
            "ticker": "KXBTCD-26MAY31-B90000",
            "title": "BTC below $90000",
            "subtitle": "Below $90,000",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.44",
            "no_bid_dollars": "0.56",
            "no_ask_dollars": "0.60",
            "close_time": "2026-05-31T23:59:00Z",
        }
        raw.update(overrides)
        return raw

    def test_terminal_below_carries_direction(self) -> None:
        # strike_type "less" with no "ever" → terminal below.
        tick = normalize_kalshi_tick(self._raw(
            strike_type="less",
            rules_primary="If the price of BTC at expiry is below 90000, resolves Yes.",
        ))
        assert tick.direction == "below"
        assert tick.product_type == "terminal"

    def test_minmon_barrier(self) -> None:
        tick = normalize_kalshi_tick(self._raw(
            ticker="KXBTCMINMON-BTC-26MAY31-7000000",
            strike_type="less",
            yes_sub_title="Below $70,000.00",
            rules_primary="If BTC is ever below $70000.00, resolves Yes.",
        ))
        assert tick.direction == "below"
        assert tick.product_type == "one_touch"

    def test_quote_inherits_direction_and_product_type(self) -> None:
        tick = normalize_kalshi_tick(self._raw(strike_type="less"))
        quote = pm_tick_to_probability_quote(tick)
        assert quote is not None
        assert quote.direction == "below"
        assert quote.product_type == "terminal"


# ── Polymarket hard denylist (stopgap, independent of the classifier) ────────

# The barrier-class + range BTC markets enumerated in the polarity audit
# (outputs/fix_polarity_barrier_report.md section 4).  The genuine terminal
# markets are intentionally NOT denylisted.
_DENYLISTED_QUESTIONS = [
    "Will Bitcoin dip to $70,000 May 25-31?",                               # one-touch down (LIVE phantom)
    "Will Bitcoin reach $150,000 by December 31, 2026?",                    # one-touch up
    "Will the price of Bitcoin be between $64,000 and $66,000 on June 4?",  # range
]
_RETAINED_QUESTIONS = [
    "Will the price of Bitcoin be less than $68,000 on May 31?",  # terminal below
    "Will the price of Bitcoin be above $84,000 on May 31?",      # terminal above
]


class TestPolymarketDenylist:
    @pytest.mark.parametrize("question", _DENYLISTED_QUESTIONS)
    def test_denylisted_question_predicate(self, question: str) -> None:
        # The denylist predicate is a pure function of the identifiers a tick
        # carries -- it does not depend on the free-text classifier.
        assert is_polymarket_denylisted("0xwhatever", question) is True

    @pytest.mark.parametrize("question", _RETAINED_QUESTIONS)
    def test_retained_question_not_denylisted(self, question: str) -> None:
        assert is_polymarket_denylisted("0xwhatever", question) is False

    @pytest.mark.parametrize("question", _DENYLISTED_QUESTIONS)
    def test_denylisted_market_forced_off_terminal_pricer(self, question: str) -> None:
        # A denylisted market is tracked but excluded: it must never normalize
        # to a terminal product (which is what the pricer/signal path trades).
        raw = {
            "condition_id": "0xdeny",
            "question": question,
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.40", "0.60"],
            "endDate": "2026-12-31T00:00:00Z",
        }
        tick = normalize_polymarket_tick(raw)
        assert tick.product_type != "terminal"

    def test_denylist_normalizes_whitespace_and_case(self) -> None:
        # Extra whitespace / casing must not let a phantom slip through.
        raw = {
            "condition_id": "0xspacey",
            "question": "  will   BITCOIN dip to $70,000 MAY 25-31?  ",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.40", "0.60"],
            "endDate": "2026-12-31T00:00:00Z",
        }
        tick = normalize_polymarket_tick(raw)
        assert tick.product_type == "one_touch"

    def test_condition_id_denylist_path(self) -> None:
        # condition_id matching works even when the question text is benign
        # (proves the denylist overrides whatever the classifier would decide).
        from btc_pm_arb.feeds import normalizer as _nz

        cid = "0xCONDITION-PHANTOM"
        original = _nz._PM_DENYLIST_CONDITION_IDS
        _nz._PM_DENYLIST_CONDITION_IDS = frozenset({cid})
        try:
            raw = {
                "condition_id": cid,
                "question": "Will Bitcoin be above $84,000 on May 31?",
                "outcomes": ["Yes", "No"],
                "outcomePrices": ["0.40", "0.60"],
                "endDate": "2026-05-31T00:00:00Z",
            }
            tick = _nz.normalize_polymarket_tick(raw)
            assert tick.product_type == "one_touch"
        finally:
            _nz._PM_DENYLIST_CONDITION_IDS = original

    def test_non_denylisted_terminal_above_unchanged(self) -> None:
        # Sanity: an ordinary terminal-above market is not collaterally excluded.
        raw = {
            "condition_id": "0xok",
            "question": "Will BTC be above $100,000 on Dec 31?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.30", "0.70"],
            "endDate": "2026-12-31T00:00:00Z",
        }
        tick = normalize_polymarket_tick(raw)
        assert tick.product_type == "terminal"


# ── Polymarket free-text question classifier ─────────────────────────────────

# Every enumerated audit case (outputs/fix_polarity_barrier_report.md section 4)
# as (question, expected_direction, expected_product_type).  Barriers + range
# are excluded; the terminal-below is retained-and-polarized; terminal-above is
# unchanged.  Synthetic phrasing variants exercise the full "dip to" (~5),
# "reach" (~15) and "between" (6) populations the recordings carried.
_PM_CLASSIFY_CASES = [
    # 5 "dip to" one-touch-down barriers (LIVE phantom class) -> excluded
    ("Will Bitcoin dip to $70,000 May 25-31?", "below", "one_touch"),
    ("Will Bitcoin dip to $80,000 this week?", "below", "one_touch"),
    ("Will BTC fall to $60,000 before June?", "below", "one_touch"),
    ("Will Bitcoin drop to $50,000 in May?", "below", "one_touch"),
    ("Will BTC dip to $90,000 on any day in June?", "below", "one_touch"),
    # "reach" one-touch-up barriers (latent) -> excluded
    ("Will Bitcoin reach $150,000 by December 31, 2026?", "above", "one_touch"),
    ("Will BTC hit $200,000 in 2026?", "above", "one_touch"),
    ("Will Bitcoin touch $120,000 this year?", "above", "one_touch"),
    ("Will BTC reach an all-time high in 2026?", "above", "one_touch"),
    # "between" range / band products -> excluded
    ("Will the price of Bitcoin be between $64,000 and $66,000 on June 4?", "above", "range"),
    ("Will Bitcoin be between $100,000 and $110,000 on Dec 31?", "above", "range"),
    # terminal below ("less than ... on [date]") -> RETAINED, below
    ("Will the price of Bitcoin be less than $68,000 on May 31?", "below", "terminal"),
    ("Will BTC be under $90,000 on June 30?", "below", "terminal"),
    # terminal above ("above/over/exceed ... on [date]") -> RETAINED, above
    ("Will the price of Bitcoin be above $84,000 on May 31?", "above", "terminal"),
    ("Will BTC be over $100,000 on Dec 31?", "above", "terminal"),
    ("Will Bitcoin exceed $120,000 by Dec 31?", "above", "terminal"),
]


class TestClassifyPolymarket:
    @pytest.mark.parametrize("question,direction,product_type", _PM_CLASSIFY_CASES)
    def test_product_type(self, question: str, direction: str, product_type: str) -> None:
        assert classify_polymarket_product_type(question) == product_type

    @pytest.mark.parametrize("question,direction,product_type", _PM_CLASSIFY_CASES)
    def test_direction(self, question: str, direction: str, product_type: str) -> None:
        assert classify_polymarket_direction(question) == direction

    # ── FAIL CLOSED ──────────────────────────────────────────────────────────
    @pytest.mark.parametrize(
        "question",
        [
            "",
            "asdf qwerty zxcv",                                  # garbled
            "Will Bitcoin go bananas in 2026?",                 # no comparator
            "Will BTC be above $90,000?",                       # comparator, no date anchor
            "Will BTC be above $80,000 but below $90,000 on June 1?",  # two-sided / ambiguous
            "What happens to Bitcoin this year?",              # vague
        ],
    )
    def test_fail_closed_excludes_not_terminal_above(self, question: str) -> None:
        # The cardinal guardrail: an ambiguous/unmatched question must NOT
        # become a tradable terminal -- it is excluded (one_touch).
        assert classify_polymarket_product_type(question) == "one_touch"

    def test_existing_above_terminal_still_terminal(self) -> None:
        # Regression: the base PM fixture used across the suite stays terminal.
        assert classify_polymarket_product_type(
            "Will BTC be above $100,000 by June 30?"
        ) == "terminal"


class TestNormalizePolymarketPolarityProductType:
    """normalize_polymarket_tick carries classifier output onto the tick/quote."""

    def _raw(self, question: str, **overrides: object) -> dict:
        raw = {
            "condition_id": "0xpm",
            "question": question,
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.40", "0.60"],
            "endDate": "2026-05-31T00:00:00Z",
        }
        raw.update(overrides)
        return raw

    def test_terminal_below_retained_and_polarized(self) -> None:
        tick = normalize_polymarket_tick(
            self._raw("Will the price of Bitcoin be less than $68,000 on May 31?")
        )
        assert tick.direction == "below"
        assert tick.product_type == "terminal"
        quote = pm_tick_to_probability_quote(tick)
        assert quote is not None
        assert quote.direction == "below"
        assert quote.product_type == "terminal"

    def test_dip_to_excluded(self) -> None:
        tick = normalize_polymarket_tick(
            self._raw("Will Bitcoin dip to $70,000 May 25-31?")
        )
        assert tick.product_type == "one_touch"
        assert tick.direction == "below"

    def test_between_is_range(self) -> None:
        tick = normalize_polymarket_tick(
            self._raw("Will the price of Bitcoin be between $64,000 and $66,000 on June 4?")
        )
        assert tick.product_type == "range"

    def test_terminal_above_retained(self) -> None:
        tick = normalize_polymarket_tick(
            self._raw("Will the price of Bitcoin be above $84,000 on May 31?")
        )
        assert tick.direction == "above"
        assert tick.product_type == "terminal"
