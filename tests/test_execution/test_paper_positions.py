"""Tests for execution/paper_positions.py — in-memory paper-position tracker.

Scope (Round 8 Commit 1, M2M only — settlement portion deferred to Commit 2
along with the Kalshi settlement poller):

* record_fill: new positions, weighted-average entry, fee accumulation
* mark_to_market: side-aware mid (yes_mid for YES, no_mid for NO),
  multiple positions, only matching contracts updated
* mark_to_market freshness contract: last_mark_at bumps on every observation
  even when the mid is unchanged or one-sided (the load-bearing
  clarification from the Round 8 plan review)
* unrealized_pnl across price moves
* hedged positions: long YES and long NO on the same contract are kept
  as separate triples
* performance_summary aggregations
* Replay/idempotency invariant: incremental tracker == replay tracker
  (the load-bearing test for Round 8's "data accumulates across restarts"
  promise)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from btc_pm_arb.execution.paper_ledger import (
    BookLevel,
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.execution.paper_positions import PaperPosition, PaperPositionTracker
from btc_pm_arb.models import DataSource, PredictionMarketTick


# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=14)


def _make_order(
    *,
    client_order_id: str = "co-1",
    contract_id: str = "KXBTC-26MAY-100000",
    side: str = "yes",
    size_usd: float = 200.0,
    limit_price: float = 0.45,
    platform: DataSource = DataSource.KALSHI,
) -> PaperOrderRecord:
    return PaperOrderRecord(
        client_order_id=client_order_id,
        signal_fingerprint=f"{contract_id}:{side}:{_EXPIRY.isoformat()}",
        created_at=_NOW,
        platform=platform,
        contract_id=contract_id,
        side=side,
        size_usd=size_usd,
        limit_price=limit_price,
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
        expiry=_EXPIRY,
    )


def _make_fill(
    *,
    client_order_id: str = "co-1",
    fill_price: float = 0.45,
    fill_size_usd: float = 200.0,
    outcome: str = "full",
    fees_usd: float = 0.0,
    filled_at: datetime | None = None,
) -> PaperFillRecord:
    return PaperFillRecord(
        client_order_id=client_order_id,
        filled_at=filled_at or _NOW,
        fill_price=fill_price if outcome != "no_fill" else None,
        fill_size_usd=fill_size_usd if outcome != "no_fill" else 0.0,
        fill_outcome=outcome,
        simulator_reason="marketable_against_book"
        if outcome != "no_fill"
        else "non_marketable_dropped",
        fees_usd=fees_usd,
    )


def _make_tick(
    *,
    contract_id: str = "KXBTC-26MAY-100000",
    yes_bid: float | None = 0.49,
    yes_ask: float | None = 0.51,
    no_bid: float | None = 0.49,
    no_ask: float | None = 0.51,
    timestamp: datetime | None = None,
    source: DataSource = DataSource.KALSHI,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=source,
        contract_id=contract_id,
        question="BTC > $100k?",
        strike=100_000.0,
        expiry=_EXPIRY,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=timestamp or _NOW,
    )


# ── record_fill ───────────────────────────────────────────────────────────────


def test_record_fill_creates_new_position():
    tracker = PaperPositionTracker()
    pos = tracker.record_fill(order_record=_make_order(), fill_record=_make_fill())
    assert pos is not None
    assert pos.platform == DataSource.KALSHI
    assert pos.contract_id == "KXBTC-26MAY-100000"
    assert pos.side == "yes"
    assert pos.filled_size_usd == pytest.approx(200.0)
    assert pos.entry_price == pytest.approx(0.45)
    assert pos.order_ids == ["co-1"]


def test_record_fill_no_fill_returns_none_no_position_created():
    tracker = PaperPositionTracker()
    pos = tracker.record_fill(
        order_record=_make_order(),
        fill_record=_make_fill(outcome="no_fill"),
    )
    assert pos is None
    assert tracker.all_positions() == []


def test_record_fill_weighted_average_entry_across_two_fills():
    """Two fills on the same triple weighted-average their entry prices by size."""
    tracker = PaperPositionTracker()
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-1"),
        fill_record=_make_fill(client_order_id="co-1", fill_price=0.40, fill_size_usd=100.0),
    )
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-2"),
        fill_record=_make_fill(client_order_id="co-2", fill_price=0.50, fill_size_usd=300.0),
    )

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.filled_size_usd == pytest.approx(400.0)
    # (0.40 * 100 + 0.50 * 300) / 400 = 0.475
    assert pos.entry_price == pytest.approx(0.475)
    assert pos.order_ids == ["co-1", "co-2"]


def test_record_fill_accumulates_fees():
    tracker = PaperPositionTracker()
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-1"),
        fill_record=_make_fill(client_order_id="co-1", fees_usd=0.01),
    )
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-2"),
        fill_record=_make_fill(client_order_id="co-2", fees_usd=0.02),
    )
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.fees_usd == pytest.approx(0.03)


# ── Hedged positions kept separate ────────────────────────────────────────────


def test_hedged_yes_and_no_kept_as_separate_positions():
    """Long YES and long NO on the same contract live as distinct triples."""
    tracker = PaperPositionTracker()
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-1", side="yes"),
        fill_record=_make_fill(client_order_id="co-1", fill_price=0.45),
    )
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-2", side="no"),
        fill_record=_make_fill(client_order_id="co-2", fill_price=0.57),
    )
    assert len(tracker.all_positions()) == 2
    yes_pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    no_pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "no")
    assert yes_pos is not None and no_pos is not None
    assert yes_pos.entry_price == pytest.approx(0.45)
    assert no_pos.entry_price == pytest.approx(0.57)


# ── mark_to_market ────────────────────────────────────────────────────────────


def test_mark_to_market_updates_yes_position_with_yes_mid():
    tracker = PaperPositionTracker()
    tracker.record_fill(order_record=_make_order(side="yes"), fill_record=_make_fill())
    tick = _make_tick(yes_bid=0.49, yes_ask=0.51, timestamp=_NOW + timedelta(seconds=5))
    tracker.mark_to_market([tick])

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.current_mid == pytest.approx(0.50)
    assert pos.last_mark_at == tick.timestamp


def test_mark_to_market_updates_no_position_with_no_mid():
    """A NO position uses ``no_mid``, not ``yes_mid``."""
    tracker = PaperPositionTracker()
    tracker.record_fill(
        order_record=_make_order(side="no"),
        fill_record=_make_fill(fill_price=0.57),
    )
    # yes_mid = 0.50; no_mid should be 0.50 too if symmetric, but make
    # them different to confirm we're using the right side.
    tick = _make_tick(
        yes_bid=0.49,
        yes_ask=0.51,
        no_bid=0.55,
        no_ask=0.59,
    )
    tracker.mark_to_market([tick])

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "no")
    assert pos is not None
    assert pos.current_mid == pytest.approx(0.57)   # (0.55 + 0.59) / 2


def test_mark_to_market_no_tick_for_contract_leaves_position_untouched():
    tracker = PaperPositionTracker()
    tracker.record_fill(order_record=_make_order(), fill_record=_make_fill())
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    initial_mark = pos.last_mark_at

    other_tick = _make_tick(contract_id="KXBTC-DIFFERENT-CONTRACT")
    tracker.mark_to_market([other_tick])

    assert pos.current_mid is None
    assert pos.last_mark_at == initial_mark   # untouched


def test_mark_to_market_multiple_positions_only_matching_updated():
    tracker = PaperPositionTracker()
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-1", contract_id="KXBTC-A"),
        fill_record=_make_fill(client_order_id="co-1"),
    )
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-2", contract_id="KXBTC-B"),
        fill_record=_make_fill(client_order_id="co-2"),
    )
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-3", contract_id="KXBTC-C"),
        fill_record=_make_fill(client_order_id="co-3"),
    )

    tracker.mark_to_market([
        _make_tick(contract_id="KXBTC-A", yes_bid=0.59, yes_ask=0.61),
        _make_tick(contract_id="KXBTC-B", yes_bid=0.39, yes_ask=0.41),
        # No tick for KXBTC-C
    ])

    pos_a = tracker.get(DataSource.KALSHI, "KXBTC-A", "yes")
    pos_b = tracker.get(DataSource.KALSHI, "KXBTC-B", "yes")
    pos_c = tracker.get(DataSource.KALSHI, "KXBTC-C", "yes")
    assert pos_a is not None and pos_b is not None and pos_c is not None
    assert pos_a.current_mid == pytest.approx(0.60)
    assert pos_b.current_mid == pytest.approx(0.40)
    assert pos_c.current_mid is None


# ── Freshness-of-observation contract (the load-bearing clarification) ────────


def test_mark_to_market_flat_mid_still_bumps_last_mark_at():
    """A second tick with identical mid still bumps last_mark_at — freshness
    is observation-based, not price-change-based.  Per the Round 8 plan-review
    clarification: a contract that ticks with a flat mid should not look stale.
    """
    tracker = PaperPositionTracker()
    tracker.record_fill(order_record=_make_order(), fill_record=_make_fill())

    t1 = _make_tick(yes_bid=0.49, yes_ask=0.51, timestamp=_NOW + timedelta(seconds=5))
    tracker.mark_to_market([t1])
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.current_mid == pytest.approx(0.50)
    first_mark_at = pos.last_mark_at

    # Identical mid, later timestamp
    t2 = _make_tick(yes_bid=0.49, yes_ask=0.51, timestamp=_NOW + timedelta(seconds=10))
    tracker.mark_to_market([t2])
    assert pos.current_mid == pytest.approx(0.50)
    assert pos.last_mark_at == t2.timestamp
    assert pos.last_mark_at != first_mark_at


def test_mark_to_market_one_sided_book_bumps_last_mark_at_leaves_mid():
    """A one-sided tick (yes_ask is None → yes_mid is None) still bumps
    last_mark_at; current_mid is preserved at its previous value.
    """
    tracker = PaperPositionTracker()
    tracker.record_fill(order_record=_make_order(), fill_record=_make_fill())

    # Establish a prior mid
    t1 = _make_tick(yes_bid=0.49, yes_ask=0.51, timestamp=_NOW + timedelta(seconds=5))
    tracker.mark_to_market([t1])
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.current_mid == pytest.approx(0.50)

    # One-sided tick: yes_ask is None
    t2 = _make_tick(yes_bid=0.49, yes_ask=None, timestamp=_NOW + timedelta(seconds=10))
    tracker.mark_to_market([t2])
    assert pos.current_mid == pytest.approx(0.50)        # preserved
    assert pos.last_mark_at == t2.timestamp              # bumped


def test_mark_to_market_takes_latest_tick_per_contract():
    """When the input list contains multiple ticks for the same contract,
    only the latest (by timestamp) is applied.
    """
    tracker = PaperPositionTracker()
    tracker.record_fill(order_record=_make_order(), fill_record=_make_fill())

    older = _make_tick(yes_bid=0.39, yes_ask=0.41, timestamp=_NOW + timedelta(seconds=5))
    newer = _make_tick(yes_bid=0.59, yes_ask=0.61, timestamp=_NOW + timedelta(seconds=10))
    # Pass in NON-chronological order to make sure the tracker sorts correctly
    tracker.mark_to_market([newer, older])

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.current_mid == pytest.approx(0.60)
    assert pos.last_mark_at == newer.timestamp


# ── Unrealized P&L across price moves ─────────────────────────────────────────


def test_unrealized_pnl_tracks_price_moves():
    tracker = PaperPositionTracker()
    tracker.record_fill(
        order_record=_make_order(),
        fill_record=_make_fill(fill_price=0.40, fill_size_usd=200.0),
    )
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.unrealized_pnl == 0.0   # No mid yet

    # Mid moves up — unrealized profit
    tracker.mark_to_market([_make_tick(yes_bid=0.44, yes_ask=0.46)])
    # (0.45 - 0.40) * 200 = 10.0
    assert pos.unrealized_pnl == pytest.approx(10.0)

    # Mid moves further up
    tracker.mark_to_market([
        _make_tick(yes_bid=0.54, yes_ask=0.56, timestamp=_NOW + timedelta(seconds=10))
    ])
    # (0.55 - 0.40) * 200 = 30.0
    assert pos.unrealized_pnl == pytest.approx(30.0)

    # Mid moves down — unrealized loss
    tracker.mark_to_market([
        _make_tick(yes_bid=0.34, yes_ask=0.36, timestamp=_NOW + timedelta(seconds=20))
    ])
    # (0.35 - 0.40) * 200 = -10.0
    assert pos.unrealized_pnl == pytest.approx(-10.0)


def test_unrealized_pnl_zero_when_position_closed():
    """A closed position reports unrealized_pnl == 0 even with a current_mid."""
    tracker = PaperPositionTracker()
    tracker.record_fill(order_record=_make_order(), fill_record=_make_fill())
    tracker.mark_to_market([_make_tick(yes_bid=0.59, yes_ask=0.61)])
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.unrealized_pnl != 0.0

    # Close the position
    settlement = PaperSettlementRecord(
        client_order_id="co-1",
        contract_id="KXBTC-26MAY-100000",
        platform=DataSource.KALSHI,
        side="yes",
        settled_at=_NOW + timedelta(days=14),
        settlement_price=1.0,
        payout_price=1.0,
        entry_price=0.45,
        size_usd=200.0,
        realized_pnl=110.0,
        outcome="win",
        theoretical_edge=0.05,
        expiry=_EXPIRY,
    )
    tracker.settle(settlement)
    assert pos.closed
    assert pos.unrealized_pnl == 0.0


# ── performance_summary ───────────────────────────────────────────────────────


def test_performance_summary_with_no_positions():
    tracker = PaperPositionTracker()
    summary = tracker.performance_summary()
    assert summary["total_paper_positions"] == 0
    assert summary["open_paper_positions"] == 0
    assert summary["closed_paper_positions"] == 0
    assert summary["win_count"] == 0
    assert summary["loss_count"] == 0
    assert summary["win_rate"] == 0.0
    assert summary["total_realized_pnl"] == 0.0


def test_performance_summary_aggregates_open_and_closed():
    tracker = PaperPositionTracker()
    # Open position 1 with unrealized profit
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-1", contract_id="KXBTC-A"),
        fill_record=_make_fill(client_order_id="co-1", fill_price=0.40),
    )
    tracker.mark_to_market([_make_tick(contract_id="KXBTC-A", yes_bid=0.49, yes_ask=0.51)])

    # Open position 2, then settle as a win
    tracker.record_fill(
        order_record=_make_order(client_order_id="co-2", contract_id="KXBTC-B"),
        fill_record=_make_fill(client_order_id="co-2", fill_price=0.45),
    )
    tracker.settle(PaperSettlementRecord(
        client_order_id="co-2",
        contract_id="KXBTC-B",
        platform=DataSource.KALSHI,
        side="yes",
        settled_at=_NOW + timedelta(days=14),
        settlement_price=1.0,
        payout_price=1.0,
        entry_price=0.45,
        size_usd=200.0,
        realized_pnl=110.0,
        outcome="win",
        theoretical_edge=0.05,
        expiry=_EXPIRY,
    ))

    summary = tracker.performance_summary()
    assert summary["total_paper_positions"] == 2
    assert summary["open_paper_positions"] == 1
    assert summary["closed_paper_positions"] == 1
    assert summary["win_count"] == 1
    assert summary["loss_count"] == 0
    assert summary["win_rate"] == pytest.approx(1.0)
    assert summary["total_realized_pnl"] == pytest.approx(110.0)
    # Position 1: (0.50 - 0.40) * 200 = 20.0
    assert summary["total_unrealized_pnl"] == pytest.approx(20.0)
    assert summary["total_paper_pnl"] == pytest.approx(130.0)
    assert summary["total_exposure_usd"] == pytest.approx(200.0)


# ── Replay/idempotency invariant (load-bearing for "data accumulates") ────────


def test_replay_from_disk_reconstructs_identical_state(tmp_path: Path):
    """The load-bearing Round 8 invariant: tracker state built from event
    records via incremental calls is identical to state built by replaying
    the on-disk JSONL files into a fresh tracker.  Without this, "weeks-to-
    months of would-be-trade outcomes across restarts" is aspirational, not
    guaranteed.
    """
    ledger = PaperLedger(tmp_path)
    incremental = PaperPositionTracker()

    # Scenario: 5 orders (one no-fill, one settled, three open), 5 fills,
    # 1 settlement.
    scenarios = [
        ("co-1", "KXBTC-A", "yes", "full", 0.45, 200.0),
        ("co-2", "KXBTC-B", "yes", "full", 0.30, 100.0),
        ("co-3", "KXBTC-C", "no",  "full", 0.55, 150.0),
        ("co-4", "KXBTC-D", "yes", "no_fill", None, 0.0),
        ("co-5", "KXBTC-E", "yes", "full", 0.60, 250.0),
    ]
    for cid, contract, side, outcome, fill_px, fill_sz in scenarios:
        order = _make_order(
            client_order_id=cid,
            contract_id=contract,
            side=side,
            limit_price=fill_px if fill_px is not None else 0.45,
        )
        ledger.append_order(order)
        fill = _make_fill(
            client_order_id=cid,
            outcome=outcome,
            fill_price=fill_px if fill_px is not None else 0.45,
            fill_size_usd=fill_sz,
            fees_usd=0.005,
        )
        ledger.append_fill(fill)
        incremental.record_fill(order_record=order, fill_record=fill)

    # Settle co-2
    settlement = PaperSettlementRecord(
        client_order_id="co-2",
        contract_id="KXBTC-B",
        platform=DataSource.KALSHI,
        side="yes",
        settled_at=_NOW + timedelta(days=14),
        settlement_price=0.0,
        payout_price=0.0,
        entry_price=0.30,
        size_usd=100.0,
        realized_pnl=-30.0,
        fees_usd=0.005,
        outcome="loss",
        theoretical_edge=0.04,
        expiry=_EXPIRY,
    )
    ledger.append_settlement(settlement)
    incremental.settle(settlement)

    # Build replay tracker against the same ledger files
    replay = PaperPositionTracker()
    fresh_ledger = PaperLedger(tmp_path)
    replay.replay_from_disk(fresh_ledger)

    # The load-bearing assertion: deterministic state representations equal.
    assert replay.snapshot_state() == incremental.snapshot_state()


def test_replay_fill_without_matching_order_skipped(tmp_path: Path):
    """An orphan fill record is skipped with a warning, not crashed on."""
    ledger = PaperLedger(tmp_path)

    # Append an order, then a fill that references a *different* client_order_id
    ledger.append_order(_make_order(client_order_id="co-1"))
    ledger.append_fill(_make_fill(client_order_id="co-1"))
    ledger.append_fill(_make_fill(client_order_id="co-orphan"))

    tracker = PaperPositionTracker()
    fresh = PaperLedger(tmp_path)
    tracker.replay_from_disk(fresh)

    # Only the matched fill produced a position
    assert len(tracker.all_positions()) == 1
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.order_ids == ["co-1"]


# ── snapshot() rendering for dashboard ────────────────────────────────────────


def test_position_snapshot_keys_and_types():
    tracker = PaperPositionTracker()
    tracker.record_fill(order_record=_make_order(), fill_record=_make_fill())
    tracker.mark_to_market([_make_tick(yes_bid=0.49, yes_ask=0.51)])

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    snap = pos.snapshot()
    expected_keys = {
        "platform", "contract_id", "side", "expiry",
        "filled_size_usd", "entry_price", "current_mid", "last_mark_at",
        "unrealized_pnl", "realized_pnl", "fees_usd", "total_pnl",
        "closed", "settlement_price", "opened_at", "updated_at", "order_ids",
    }
    assert set(snap.keys()) == expected_keys
    assert snap["platform"] == "kalshi"   # enum value, not enum instance
    assert isinstance(snap["last_mark_at"], str)   # iso string for JSON
