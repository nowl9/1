"""C2 regression tests: Kalshi _build_tick must forward executable book depth.

Defect pinned here (Phase 1 diagnosis 2026-06-11): _build_tick aggregated
the orderbook_fp payload to four best-bid/ask floats and dropped the levels,
so every Kalshi tick carried order_book_yes=[] / order_book_no=[].  The
empty_book filter then blocked every terminal Kalshi signal BEFORE the edge
floors (masking the true rejection reason), edge's fill-adjusted walk always
fell back, and FillSimulator(book_walk=True) walked empty books on the only
venue we execute on.

Fixture provenance: _REAL_FRAME below is the byte-for-byte recorded raw
HTTP body for /markets/KXBTCMINMON-BTC-26JUN30-5750000/orderbook, frame
ts=2026-06-11T01:38:57.210487+00:00, from
data/recordings/kalshi/2026-06-11/frames-01.jsonl.gz (22 NO levels,
19 YES levels) -- the exact contract whose live tick re-confirmed the
empty-depth defect.

Post-migration orderbook_fp shape ONLY: the Phase 1 audit found 28,773 of
28,773 recorded orderbook frames across all four banked windows are
orderbook_fp / *_dollars.  No pre-migration [price_cents, qty] frame exists
on disk, so there is deliberately no legacy-shape fixture and no legacy
branch in _derive_ask_depth -- do not add either speculatively.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.execution.fill_simulator import BookSnapshot, FillSimulator
from btc_pm_arb.feeds.kalshi import _build_tick, _derive_ask_depth
from btc_pm_arb.feeds.normalizer import pm_tick_to_probability_quote
from btc_pm_arb.models import DataSource
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.signals.edge import EdgeResult, fill_adjusted_price
from btc_pm_arb.signals.filters import SignalFilter
from btc_pm_arb.signals.matcher import MatchResult

# Byte-for-byte recorded raw body (see module docstring for provenance).
_REAL_FRAME = (
    '{"orderbook_fp":{"no_dollars":[["0.0100","10488.92"],["0.0300","1.59"],'
    '["0.0500","5005.00"],["0.0800","12.19"],["0.0900","88.00"],'
    '["0.1000","3833.88"],["0.1100","6.83"],["0.1400","500.00"],'
    '["0.1600","11.99"],["0.1700","132.93"],["0.1800","6.40"],'
    '["0.2000","2000.00"],["0.2100","164.38"],["0.2500","26.34"],'
    '["0.2600","12.56"],["0.3000","1753.53"],["0.3100","325.00"],'
    '["0.3800","5.00"],["0.4100","10.00"],["0.4500","5.00"],'
    '["0.4700","17.06"],["0.4800","1296.19"]],"yes_dollars":'
    '[["0.0100","17920.00"],["0.0200","15220.00"],["0.0300","5000.00"],'
    '["0.0500","5800.00"],["0.0600","78.15"],["0.0700","1000.00"],'
    '["0.0800","1000.00"],["0.0900","80.00"],["0.1000","77.70"],'
    '["0.1500","1000.00"],["0.1600","1000.00"],["0.2700","1000.00"],'
    '["0.2800","1155.17"],["0.2900","1000.00"],["0.3000","1000.00"],'
    '["0.3100","229.59"],["0.4400","13.00"],["0.4600","650.00"],'
    '["0.4700","334.17"]]}}'
)

_TICKER = "KXBTCMINMON-BTC-26JUN30-5750000"
_META = {
    "title": "Will BTC dip to $57,500 before Jul 1?",
    "subtitle": "BTC min below $57,500",
    "close_time": "2026-06-30T14:00:00Z",
}


def _real_book() -> dict:
    return json.loads(_REAL_FRAME)["orderbook_fp"]


def _real_tick():
    tick = _build_tick(_TICKER, _META, _real_book())
    assert tick is not None
    return tick


# ── The empty-list regression pin ─────────────────────────────────────────────


class TestRealRecordedFrameDepth:
    """A depth-bearing recorded payload must NEVER again yield empty books.

    Before the 2026-06-11 fix this exact frame produced
    order_book_yes=[] / order_book_no=[] (the silent default in
    normalize_kalshi_tick when _build_tick forwarded no depth keys).
    """

    def test_depth_lists_are_not_empty(self) -> None:
        tick = _real_tick()
        assert tick.order_book_yes, (
            "order_book_yes is empty for a 22-NO-level recorded payload -- "
            "the pre-2026-06-11 silent-default regression is back"
        )
        assert tick.order_book_no, (
            "order_book_no is empty for a 19-YES-level recorded payload -- "
            "the pre-2026-06-11 silent-default regression is back"
        )

    def test_yes_depth_derived_from_all_22_no_bids(self) -> None:
        tick = _real_tick()
        book = tick.order_book_yes
        assert len(book) == 22
        # Cheapest derived ask = 1 - best NO bid (0.48), carrying its qty.
        assert book[0] == (pytest.approx(0.52), pytest.approx(1296.19))
        # Most expensive = 1 - worst NO bid (0.01).
        assert book[-1] == (pytest.approx(0.99), pytest.approx(10488.92))

    def test_no_depth_derived_from_all_19_yes_bids(self) -> None:
        tick = _real_tick()
        book = tick.order_book_no
        assert len(book) == 19
        assert book[0] == (pytest.approx(0.53), pytest.approx(334.17))
        assert book[-1] == (pytest.approx(0.99), pytest.approx(17920.00))

    def test_depth_sorted_cheapest_first(self) -> None:
        tick = _real_tick()
        for book in (tick.order_book_yes, tick.order_book_no):
            prices = [p for p, _ in book]
            assert prices == sorted(prices)

    def test_depth_consistent_with_top_of_book(self) -> None:
        """The cheapest derived level IS the top-of-book ask -- the depth and
        the existing best-ask derivation must agree at level 0."""
        tick = _real_tick()
        assert tick.order_book_yes[0][0] == pytest.approx(tick.yes_ask)
        assert tick.order_book_no[0][0] == pytest.approx(tick.no_ask)

    def test_sizes_all_positive(self) -> None:
        tick = _real_tick()
        for book in (tick.order_book_yes, tick.order_book_no):
            assert all(s > 0.0 for _, s in book)


class TestDeriveAskDepthEdgeCases:
    def test_one_sided_book_populates_only_derived_side(self) -> None:
        """yes_dollars=[] (real prod observation): the YES book still derives
        from the NO bids; the NO book is honestly empty."""
        book_fp = {
            "yes_dollars": [],
            "no_dollars": [["0.0100", "33383.00"], ["0.9900", "16456.00"]],
        }
        tick = _build_tick("KXBTC-26DEC31-B100000", _META, book_fp)
        assert tick is not None
        assert tick.order_book_yes == [(0.01, 16456.00), (0.99, 33383.00)]
        assert tick.order_book_no == []

    def test_malformed_level_skipped_individually(self) -> None:
        """One bad level must not blank the whole book."""
        levels = [
            ["0.4800", "1296.19"],
            ["bogus"],                  # too short / unparseable price
            ["0.3000", "not-a-number"],  # unparseable qty
            ["0.2000", "0.00"],          # zero size -- nothing to lift
            ["0.4500", "5.00"],
        ]
        assert _derive_ask_depth(levels) == [
            (pytest.approx(0.52), pytest.approx(1296.19)),
            (pytest.approx(0.55), pytest.approx(5.00)),
        ]

    def test_empty_and_none_inputs_yield_empty(self) -> None:
        assert _derive_ask_depth([]) == []
        assert _derive_ask_depth(None) == []  # type: ignore[arg-type]


# ── The walkers consume the derived depth (conventions match) ─────────────────


class TestWalkersConsumeDerivedDepth:
    def test_edge_walk_1_fill_adjusted_price(self) -> None:
        """edge.fill_adjusted_price (walk #1, the orders.jsonl number) walks
        the derived YES book: $200 fits inside the cheapest level."""
        tick = _real_tick()
        assert fill_adjusted_price(tick.order_book_yes, size_usd=200.0) == (
            pytest.approx(0.52)
        )

    def test_fill_simulator_walk_2_full_fill_not_empty_book(self) -> None:
        """FillSimulator (walk #2) book-walks a snapshot from the real tick.

        Before the fix this returned no_fill / reason='empty_book' for EVERY
        Kalshi order -- the only-venue-we-execute-on blindness.
        """
        tick = _real_tick()
        sim = FillSimulator(book_walk=True)
        evaluation = sim.evaluate(
            side="yes",
            limit_price=0.52,
            size_usd=200.0,
            snapshot=BookSnapshot.from_tick(tick),
        )
        assert evaluation.reason != "empty_book"
        assert evaluation.outcome == "full"
        assert evaluation.fill_price == pytest.approx(0.52)


# ── Amendment: empty_book pollution regression ────────────────────────────────

# Pinned instants -- same hermetic-clock convention as test_filters.py.
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=27)

# A TERMINAL Kalshi series (no MINMON/MAXMON in the ticker, no "ever" rules)
# so the one_touch filter does not fire ahead of the book stages.
_TERMINAL_TICKER = "KXBTCMAX150-26DEC31-T150000"
_TERMINAL_META = {
    "title": "Will BTC be above $150,000 on Dec 31?",
    "subtitle": "BTC above $150,000",
    "close_time": _EXPIRY.strftime("%Y-%m-%dT%H:%M:%SZ"),
}


def _terminal_edge(quote_override=None) -> EdgeResult:
    """A sub-floor (0.5% < 1% min_conservative_edge) but POSITIVE-edge
    Kalshi terminal EdgeResult built through the real _build_tick path."""
    tick = _build_tick(_TERMINAL_TICKER, _TERMINAL_META, _real_book())
    assert tick is not None
    assert tick.product_type == "terminal"
    tick.timestamp = _NOW
    quote = pm_tick_to_probability_quote(tick)
    assert quote is not None
    if quote_override is not None:
        quote = quote.model_copy(update=quote_override)
    entry = CacheEntry(
        strike=150_000.0,
        expiry=tick.expiry,
        bid_prob=0.55,
        ask_prob=0.59,
        mid_prob=0.57,
        source=DataSource.DERIBIT,
        timestamp=_NOW,
    )
    match = MatchResult(
        pm_tick=tick,
        pm_quote=quote,
        options_entry=entry,
        matched_strike=150_000.0,
        matched_expiry=tick.expiry,
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        is_interpolated=False,
    )
    return EdgeResult(
        match=match,
        edge_yes_mid=0.02,
        edge_no_mid=-0.02,
        edge_yes_conservative=0.005,
        edge_no_conservative=-0.02,
        adjusted_edge_yes=0.005,
        adjusted_edge_no=-0.02,
        best_side="buy_yes",
        best_conservative_edge=0.005,
        edge_history_mean=0.005,
        edge_history_std=0.01,
        edge_persistence=0.8,
        timestamp=_NOW,
    )


class TestEmptyBookPollutionRegression:
    """A sub-floor Kalshi terminal signal must reject for its TRUE reason.

    Before the fix, every Kalshi quote carried empty books, so the
    empty_book criterion (which runs BEFORE the edge floors) masked the
    real verdict: the funnel showed 'empty_book' where it should have
    shown 'conservative_edge ... < min ...'.
    """

    def test_sub_floor_terminal_rejects_for_true_reason(self) -> None:
        reason = SignalFilter().explains(_terminal_edge(), clock=lambda: _NOW)
        assert reason is not None
        assert reason.startswith("conservative_edge"), (
            f"expected the true sub-floor reason, got {reason!r}"
        )
        assert "empty_book" not in reason

    def test_counterfactual_old_defect_masked_the_true_reason(self) -> None:
        """The same edge with the pre-fix empty books rejects as empty_book --
        pinning that the depth fix is exactly what un-masks the verdict."""
        edge = _terminal_edge(
            quote_override={"order_book_yes": [], "order_book_no": []},
        )
        reason = SignalFilter().explains(edge, clock=lambda: _NOW)
        assert reason == "empty_book"
