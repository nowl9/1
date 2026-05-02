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
