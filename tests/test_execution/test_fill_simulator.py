"""Tests for execution/fill_simulator.py — paper fill evaluation logic.

Two test groups:

1. **Marketable cases** — exercise the only branch reachable in production
   (limit at-or-above best ask, full fill at ask).

2. **No-fill cases** — exercise the four defensive branches by deliberately
   constructing stale or modified snapshots.  Without these, a future
   round that introduces a slippage haircut or signal-to-fill latency
   could ship a simulator whose only reachable production branch is "fill
   at ask" and the test suite would not catch it.

See ``fill_simulator.py`` module docstring for the production-bias caveat
this test file is guarding against.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from btc_pm_arb.execution.fill_simulator import (
    BookSnapshot,
    FillEvaluation,
    FillSimulator,
)
from btc_pm_arb.execution.paper_ledger import (
    BookLevel,
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
)
from btc_pm_arb.models import DataSource


# ── Helpers ───────────────────────────────────────────────────────────────────


_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _snap(
    *,
    yes_bid: float | None = 0.43,
    yes_ask: float | None = 0.45,
    no_bid: float | None = 0.55,
    no_ask: float | None = 0.57,
    yes_levels: list[BookLevel] | None = None,
    no_levels: list[BookLevel] | None = None,
) -> BookSnapshot:
    return BookSnapshot(
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        order_book_yes=tuple(yes_levels or ()),
        order_book_no=tuple(no_levels or ()),
    )


# ── Marketable branch (the only one reached in production) ────────────────────


def test_marketable_buy_yes_above_ask_full_fill_at_ask():
    """``limit_price > yes_ask`` → full fill at ``yes_ask``."""
    sim = FillSimulator()
    ev = sim.evaluate(side="yes", limit_price=0.50, size_usd=200.0, snapshot=_snap())
    assert ev.outcome == "full"
    assert ev.fill_price == pytest.approx(0.45)
    assert ev.fill_size_usd == pytest.approx(200.0)
    assert ev.reason == "marketable_against_book"


def test_marketable_buy_yes_at_ask_full_fill():
    """``limit_price == yes_ask`` (the production case) → full fill at ask."""
    sim = FillSimulator()
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=_snap())
    assert ev.outcome == "full"
    assert ev.fill_price == pytest.approx(0.45)
    assert ev.fill_size_usd == pytest.approx(200.0)


def test_marketable_buy_no_at_ask_full_fill():
    """Symmetric production case for buy_no: ``limit_price == no_ask`` → full fill."""
    sim = FillSimulator()
    ev = sim.evaluate(side="no", limit_price=0.57, size_usd=150.0, snapshot=_snap())
    assert ev.outcome == "full"
    assert ev.fill_price == pytest.approx(0.57)
    assert ev.fill_size_usd == pytest.approx(150.0)


# ── No-fill defensive branches ────────────────────────────────────────────────


def test_stale_snapshot_buy_yes_non_marketable_dropped():
    """Stale snapshot: limit < yes_ask but >= yes_bid (defensive — unreachable
    in production with same-tick snapshot).
    """
    sim = FillSimulator()
    snap = _snap(yes_bid=0.43, yes_ask=0.50)   # ask jumped up between obs and order
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.fill_price is None
    assert ev.fill_size_usd == 0.0
    assert ev.reason == "non_marketable_dropped"


def test_modified_snapshot_buy_yes_below_book():
    """Modified snapshot: limit < yes_bid (defensive — would require a haircut)."""
    sim = FillSimulator()
    snap = _snap(yes_bid=0.43, yes_ask=0.45)
    ev = sim.evaluate(side="yes", limit_price=0.30, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.reason == "below_book"


def test_no_opposite_quote_buy_yes_no_fill():
    """One-sided book (yes_ask is None, yes_bid present) → no_opposite_quote."""
    sim = FillSimulator()
    snap = _snap(yes_bid=0.40, yes_ask=None)
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.reason == "no_opposite_quote"


def test_empty_book_buy_yes_no_fill():
    """Both yes sides None → empty_book."""
    sim = FillSimulator()
    snap = _snap(yes_bid=None, yes_ask=None)
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.reason == "empty_book"


def test_stale_snapshot_buy_no_non_marketable_dropped():
    """Symmetric stale-snapshot case for buy_no."""
    sim = FillSimulator()
    snap = _snap(no_bid=0.55, no_ask=0.62)
    ev = sim.evaluate(side="no", limit_price=0.57, size_usd=150.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.reason == "non_marketable_dropped"


def test_empty_book_buy_no_no_fill():
    """Both no sides None → empty_book for buy_no."""
    sim = FillSimulator()
    snap = _snap(no_bid=None, no_ask=None)
    ev = sim.evaluate(side="no", limit_price=0.57, size_usd=150.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.reason == "empty_book"


# ── Book depth captured-but-not-consumed (Round 8 simplification) ─────────────


def test_book_depth_is_ignored_simulator_uses_top_of_book_only():
    """Round 8 simulator uses top-of-book ``snapshot.yes_ask`` only; depth
    fields on the snapshot are persisted on the PaperOrderRecord for Round 9
    calibration but not consumed at fill-evaluation time.

    Rationale: Kalshi's ``order_book_*`` fields encode bid levels with
    asks-by-complement, while a future Polymarket integration would have
    separate bid/ask books — depth-walking semantics differ across feeds.
    The simulator stays dialect-agnostic by ignoring depth this round and
    deferring feed-specific walks to Round 9.
    """
    sim = FillSimulator()
    levels = [
        BookLevel(price=0.40, size_usd=500.0),   # would be "better" than top-of-book
        BookLevel(price=0.42, size_usd=300.0),
    ]
    snap = _snap(yes_ask=0.45, yes_levels=levels)
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "full"
    # Fill is at top-of-book yes_ask=0.45, NOT the depth-level 0.40.
    assert ev.fill_price == pytest.approx(0.45)


# ── BookSnapshot.from_order_record ────────────────────────────────────────────


def test_book_snapshot_from_order_record_round_trip():
    """``BookSnapshot.from_order_record`` extracts the right four-tuple + levels."""
    record = PaperOrderRecord(
        client_order_id="co-1",
        signal_fingerprint="fp",
        created_at=_NOW,
        platform=DataSource.KALSHI,
        contract_id="KXBTC-26MAY-100000",
        side="yes",
        size_usd=200.0,
        limit_price=0.45,
        raw_edge=0.05,
        adjusted_edge=0.04,
        confidence=0.6,
        vol_regime="normal",
        feed_staleness_ms={},
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        pm_yes_bid=0.43,
        pm_yes_ask=0.45,
        pm_no_bid=0.55,
        pm_no_ask=0.57,
        order_book_yes=[BookLevel(price=0.43, size_usd=500.0)],
        order_book_no=[BookLevel(price=0.55, size_usd=300.0)],
        expiry=_NOW,
    )
    snap = BookSnapshot.from_order_record(record)
    assert snap.yes_bid == 0.43
    assert snap.yes_ask == 0.45
    assert snap.no_bid == 0.55
    assert snap.no_ask == 0.57
    assert snap.order_book_yes == (BookLevel(price=0.43, size_usd=500.0),)
    assert snap.order_book_no == (BookLevel(price=0.55, size_usd=300.0),)


# ── BookSnapshot.from_tick ────────────────────────────────────────────────────


def test_book_snapshot_from_tick_converts_tuple_levels_to_book_levels():
    """``from_tick`` converts the tick's raw ``(price, size)`` tuples into
    ``BookLevel`` instances — the IDENTICAL conversion the placed-order path
    applies — so the rejection-path shadow fill walks the same snapshot shape.

    Structural type: a duck-typed stand-in with the tick's field names is
    accepted (mirrors ``from_order_record``'s structural protocol)."""
    from types import SimpleNamespace

    tick = SimpleNamespace(
        yes_bid=0.40,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
        order_book_yes=[(0.42, 500.0), (0.45, 300.0)],
        order_book_no=[(0.60, 250.0)],
    )
    snap = BookSnapshot.from_tick(tick)
    assert snap.yes_bid == 0.40
    assert snap.yes_ask == 0.42
    assert snap.no_bid == 0.58
    assert snap.no_ask == 0.60
    assert snap.order_book_yes == (
        BookLevel(price=0.42, size_usd=500.0),
        BookLevel(price=0.45, size_usd=300.0),
    )
    assert snap.order_book_no == (BookLevel(price=0.60, size_usd=250.0),)


def test_book_snapshot_from_tick_feeds_the_same_book_walk_as_orders():
    """A snapshot built via ``from_tick`` walks identically to one built from
    an order record — the rejection path reuses the placed-order book-walk."""
    from types import SimpleNamespace

    tick = SimpleNamespace(
        yes_bid=0.40,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
        order_book_yes=[(0.42, 120.0), (0.50, 1000.0)],
        order_book_no=[],
    )
    sim = FillSimulator(book_walk=True)
    # limit at the ask: only the $120 at 0.42 is lift-able; the 0.50 level is
    # above the limit and must NOT be crossed (no manufactured full fill).
    ev = sim.evaluate(
        side="yes", limit_price=0.42, size_usd=200.0,
        snapshot=BookSnapshot.from_tick(tick),
    )
    assert ev.outcome == "partial"
    assert ev.reason == "book_walk_partial"
    assert ev.fill_size_usd == pytest.approx(120.0)
    assert ev.fill_price == pytest.approx(0.42)


# ── build_fill_record materialisation ─────────────────────────────────────────


def test_build_fill_record_for_full_fill_round_trips_through_ledger(tmp_path):
    """The simulator's PaperFillRecord output round-trips through PaperLedger."""
    sim = FillSimulator()
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=_snap())
    record = sim.build_fill_record(
        client_order_id="co-1",
        evaluation=ev,
        filled_at=_NOW,
        fees_usd=0.0,
    )
    assert isinstance(record, PaperFillRecord)
    assert record.fill_outcome == "full"
    assert record.fill_price == pytest.approx(0.45)
    assert record.simulator_reason == "marketable_against_book"

    ledger = PaperLedger(tmp_path)
    ledger.append_fill(record)
    loaded = list(ledger.replay_fills())
    assert loaded == [record]


def test_build_fill_record_for_no_fill_carries_none_price(tmp_path):
    sim = FillSimulator()
    ev = sim.evaluate(
        side="yes",
        limit_price=0.30,
        size_usd=200.0,
        snapshot=_snap(yes_bid=0.43, yes_ask=0.45),
    )
    record = sim.build_fill_record(
        client_order_id="co-1",
        evaluation=ev,
        filled_at=_NOW,
    )
    assert record.fill_outcome == "no_fill"
    assert record.fill_price is None
    assert record.fill_size_usd == 0.0
    assert record.simulator_reason == "below_book"

    ledger = PaperLedger(tmp_path)
    ledger.append_fill(record)
    loaded = list(ledger.replay_fills())
    assert loaded == [record]


# -- Book-walking model (Fork 1, book_walk=True) -------------------------------
#
# These exercise the paper-mode fill model: walk captured depth, partial
# when thin, explicit no_fill (with reason) for an empty book or a limit
# below the whole book -- never a silent optimistic full fill.


def test_book_walk_full_fill_single_level_at_ask():
    sim = FillSimulator(book_walk=True)
    snap = _snap(yes_ask=0.45, yes_levels=[BookLevel(price=0.45, size_usd=500.0)])
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "full"
    assert ev.fill_price == pytest.approx(0.45)
    assert ev.fill_size_usd == pytest.approx(200.0)
    assert ev.reason == "book_walk_full"


def test_book_walk_full_fill_vwap_across_levels():
    """100 @ 0.40 then 300 @ 0.44; a 200 buy fills 100+100 -> VWAP 0.42."""
    sim = FillSimulator(book_walk=True)
    snap = _snap(
        yes_levels=[
            BookLevel(price=0.40, size_usd=100.0),
            BookLevel(price=0.44, size_usd=300.0),
        ],
    )
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "full"
    assert ev.fill_size_usd == pytest.approx(200.0)
    assert ev.fill_price == pytest.approx(0.42)   # (100*0.40 + 100*0.44)/200


def test_book_walk_partial_fill_when_book_thin():
    """Only 120 of a 200 buy is available at-or-below the limit -> partial."""
    sim = FillSimulator(book_walk=True)
    snap = _snap(yes_levels=[BookLevel(price=0.45, size_usd=120.0)])
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "partial"
    assert ev.fill_size_usd == pytest.approx(120.0)
    assert ev.fill_price == pytest.approx(0.45)
    assert ev.reason == "book_walk_partial"


def test_book_walk_empty_book_no_fill_with_reason():
    """No captured depth -> explicit no_fill('empty_book'), NOT a full fill
    at the top-of-book ask (the Fork 1 anti-pattern)."""
    sim = FillSimulator(book_walk=True)
    snap = _snap(yes_ask=0.45, yes_levels=[])     # ask present, depth empty
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.fill_price is None
    assert ev.fill_size_usd == 0.0
    assert ev.reason == "empty_book"


def test_book_walk_limit_below_whole_book_no_fill():
    """Depth exists but every level is above the limit -> limit_below_book."""
    sim = FillSimulator(book_walk=True)
    snap = _snap(yes_levels=[BookLevel(price=0.50, size_usd=500.0)])
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "no_fill"
    assert ev.fill_price is None
    assert ev.reason == "limit_below_book"


def test_book_walk_stops_at_levels_above_limit_partial():
    """100 @ 0.45 fillable, 300 @ 0.50 above limit -> partial 100 @ 0.45."""
    sim = FillSimulator(book_walk=True)
    snap = _snap(
        yes_levels=[
            BookLevel(price=0.45, size_usd=100.0),
            BookLevel(price=0.50, size_usd=300.0),
        ],
    )
    ev = sim.evaluate(side="yes", limit_price=0.46, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "partial"
    assert ev.fill_size_usd == pytest.approx(100.0)
    assert ev.fill_price == pytest.approx(0.45)


def test_book_walk_buy_no_walks_no_book():
    """The NO side walks order_book_no (symmetric to the YES path)."""
    sim = FillSimulator(book_walk=True)
    snap = _snap(no_levels=[BookLevel(price=0.57, size_usd=80.0)])
    ev = sim.evaluate(side="no", limit_price=0.60, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "partial"
    assert ev.fill_size_usd == pytest.approx(80.0)
    assert ev.fill_price == pytest.approx(0.57)
    assert ev.reason == "book_walk_partial"


def test_book_walk_unsorted_levels_consume_cheapest_first():
    """Defensive sort: a worst-price-first capture still fills cheapest-first."""
    sim = FillSimulator(book_walk=True)
    snap = _snap(
        yes_levels=[
            BookLevel(price=0.44, size_usd=100.0),   # listed first but pricier
            BookLevel(price=0.40, size_usd=100.0),
        ],
    )
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "full"
    assert ev.fill_price == pytest.approx(0.42)       # 100@0.40 + 100@0.44


def test_default_simulator_still_top_of_book_ignores_depth():
    """Regression: the DEFAULT simulator (book_walk=False) keeps the legacy
    top-of-book behaviour -- fills at the ask, ignoring depth levels."""
    sim = FillSimulator()                              # default: no book-walk
    snap = _snap(yes_ask=0.45, yes_levels=[BookLevel(price=0.40, size_usd=500.0)])
    ev = sim.evaluate(side="yes", limit_price=0.45, size_usd=200.0, snapshot=snap)
    assert ev.outcome == "full"
    assert ev.fill_price == pytest.approx(0.45)        # ask, NOT the 0.40 level
    assert ev.reason == "marketable_against_book"


def test_book_walk_partial_round_trips_through_ledger(tmp_path):
    """A partial fill materialises and round-trips through PaperLedger."""
    sim = FillSimulator(book_walk=True)
    ev = sim.evaluate(
        side="yes",
        limit_price=0.45,
        size_usd=200.0,
        snapshot=_snap(yes_levels=[BookLevel(price=0.45, size_usd=120.0)]),
    )
    record = sim.build_fill_record(
        client_order_id="co-partial", evaluation=ev, filled_at=_NOW,
    )
    assert record.fill_outcome == "partial"
    assert record.fill_size_usd == pytest.approx(120.0)
    ledger = PaperLedger(tmp_path)
    ledger.append_fill(record)
    assert list(ledger.replay_fills()) == [record]
