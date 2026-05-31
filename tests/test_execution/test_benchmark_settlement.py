"""Tests for execution/benchmark_settlement.py -- deterministic PM settlement.

Build step 3 (plan 3.3, 4.2; Fork 2).  Covers:
  * the pure terminal-digital benchmark model (above/below/boundary),
  * the settler closing an expired PM position and writing a
    PaperSettlementRecord through the shared build_settlement_record path,
  * DETERMINISM: the same surface (same benchmark fixing) settles to the
    same price + realized P&L twice -- the property Criterion 6 relies on,
  * the fail-safe-open skips: non-PM, not-yet-expired, missing strike,
    missing benchmark fixing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.execution.benchmark_settlement import (
    PaperBenchmarkSettler,
    benchmark_settlement_price,
)
from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
)
from btc_pm_arb.execution.paper_positions import PaperPositionTracker
from btc_pm_arb.models import DataSource


_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=7)
_AFTER_EXPIRY = _EXPIRY + timedelta(minutes=1)


# -- Builders ------------------------------------------------------------------


def _order(
    *,
    client_order_id: str = "co-pm-1",
    platform: DataSource = DataSource.POLYMARKET,
    side: str = "yes",
    strike: float | None = 100_000.0,
    direction: str = "above",
    entry_price: float = 0.42,
    adjusted_edge: float = 0.13,
) -> PaperOrderRecord:
    return PaperOrderRecord(
        client_order_id=client_order_id,
        signal_fingerprint=f"fp-{client_order_id}",
        created_at=_NOW,
        platform=platform,
        contract_id="PM-BTC-100000",
        side=side,  # type: ignore[arg-type]
        size_usd=200.0,
        limit_price=entry_price,
        raw_edge=0.14,
        adjusted_edge=adjusted_edge,
        confidence=0.7,
        vol_regime="normal",
        feed_staleness_ms={},
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        order_book_yes=[],
        order_book_no=[],
        expiry=_EXPIRY,
        strike=strike,
        direction=direction,  # type: ignore[arg-type]
    )


def _fill(order: PaperOrderRecord) -> PaperFillRecord:
    return PaperFillRecord(
        client_order_id=order.client_order_id,
        filled_at=_NOW,
        fill_price=order.limit_price,
        fill_size_usd=order.size_usd,
        fill_outcome="full",
        simulator_reason="book_walk_full",
        fees_usd=0.0,
    )


def _open_pm_position(tracker: PaperPositionTracker, order: PaperOrderRecord) -> None:
    tracker.record_fill(order_record=order, fill_record=_fill(order))


def _settler(
    tracker: PaperPositionTracker,
    ledger: PaperLedger,
    orders: dict[str, PaperOrderRecord],
    *,
    benchmark_price: float | None,
    clock_at: datetime = _AFTER_EXPIRY,
) -> PaperBenchmarkSettler:
    return PaperBenchmarkSettler(
        tracker=tracker,
        ledger=ledger,
        get_order_record=lambda cid: orders.get(cid),
        benchmark_price_fn=lambda _expiry: benchmark_price,
        clock=lambda: clock_at,
    )


# -- Pure model --------------------------------------------------------------


class TestBenchmarkModel:
    def test_above_resolves_yes_when_at_or_over_strike(self):
        assert benchmark_settlement_price(105_000.0, 100_000.0, "above") == 1.0
        assert benchmark_settlement_price(100_000.0, 100_000.0, "above") == 1.0  # boundary
        assert benchmark_settlement_price(99_999.0, 100_000.0, "above") == 0.0

    def test_below_resolves_yes_when_under_strike(self):
        assert benchmark_settlement_price(95_000.0, 100_000.0, "below") == 1.0
        assert benchmark_settlement_price(100_000.0, 100_000.0, "below") == 0.0
        assert benchmark_settlement_price(100_001.0, 100_000.0, "below") == 0.0

    def test_model_is_pure_and_repeatable(self):
        a = benchmark_settlement_price(101_234.5, 100_000.0, "above")
        b = benchmark_settlement_price(101_234.5, 100_000.0, "above")
        assert a == b == 1.0


# -- Settler -------------------------------------------------------------------


class TestSettleDue:
    def test_settles_expired_yes_winner_and_writes_record(self, tmp_path):
        tracker = PaperPositionTracker()
        ledger = PaperLedger(tmp_path)
        order = _order(side="yes", strike=100_000.0, direction="above")
        orders = {order.client_order_id: order}
        _open_pm_position(tracker, order)

        # Benchmark BTC at 105k -> above 100k -> YES wins (settlement 1.0).
        settler = _settler(tracker, ledger, orders, benchmark_price=105_000.0)
        n = settler.settle_due()
        assert n == 1

        pos = tracker.get(DataSource.POLYMARKET, "PM-BTC-100000", "yes")
        assert pos is not None and pos.closed
        assert pos.settlement_price == pytest.approx(1.0)
        # (payout 1.0 - entry 0.42) * 200 = 116.0
        assert pos.realized_pnl == pytest.approx(116.0)

        records = list(ledger.replay_settlements())
        assert len(records) == 1
        rec = records[0]
        assert rec.platform == DataSource.POLYMARKET
        assert rec.outcome == "win"
        assert rec.settlement_price == pytest.approx(1.0)
        assert rec.realized_pnl == pytest.approx(116.0)
        assert rec.theoretical_edge == pytest.approx(0.13)
        assert rec.client_order_id == order.client_order_id

    def test_settles_loser_when_benchmark_below_strike(self, tmp_path):
        tracker = PaperPositionTracker()
        ledger = PaperLedger(tmp_path)
        order = _order(side="yes", strike=100_000.0, direction="above")
        orders = {order.client_order_id: order}
        _open_pm_position(tracker, order)

        # Benchmark 95k -> below 100k -> YES loses (settlement 0.0).
        settler = _settler(tracker, ledger, orders, benchmark_price=95_000.0)
        assert settler.settle_due() == 1
        rec = list(ledger.replay_settlements())[0]
        assert rec.settlement_price == pytest.approx(0.0)
        assert rec.outcome == "loss"
        # (payout 0.0 - 0.42) * 200 = -84.0
        assert rec.realized_pnl == pytest.approx(-84.0)

    def test_deterministic_same_surface_same_settlement(self, tmp_path):
        """The property Criterion 6 relies on: two independent settler runs
        over the same position + same benchmark fixing produce identical
        settlement price and realized P&L."""
        def _run(sub: str) -> tuple[float, float]:
            tracker = PaperPositionTracker()
            ledger = PaperLedger(tmp_path / sub)
            order = _order(strike=100_000.0, direction="above")
            _open_pm_position(tracker, order)
            settler = _settler(
                tracker, ledger, {order.client_order_id: order},
                benchmark_price=103_500.0,
            )
            settler.settle_due()
            rec = list(ledger.replay_settlements())[0]
            return rec.settlement_price, rec.realized_pnl

        a_price, a_pnl = _run("run_a")
        b_price, b_pnl = _run("run_b")
        assert a_price == b_price
        assert a_pnl == b_pnl

    def test_idempotent_second_pass_settles_nothing(self, tmp_path):
        tracker = PaperPositionTracker()
        ledger = PaperLedger(tmp_path)
        order = _order()
        _open_pm_position(tracker, order)
        settler = _settler(
            tracker, ledger, {order.client_order_id: order}, benchmark_price=105_000.0,
        )
        assert settler.settle_due() == 1
        # Position now closed -> a second pass finds nothing due.
        assert settler.settle_due() == 0

    def test_skips_unexpired_position(self, tmp_path):
        tracker = PaperPositionTracker()
        ledger = PaperLedger(tmp_path)
        order = _order()
        _open_pm_position(tracker, order)
        # Clock is BEFORE expiry.
        settler = _settler(
            tracker, ledger, {order.client_order_id: order},
            benchmark_price=105_000.0, clock_at=_NOW,
        )
        assert settler.settle_due() == 0
        assert list(ledger.replay_settlements()) == []

    def test_skips_kalshi_positions(self, tmp_path):
        """Kalshi settles via its own live-oracle poller; the benchmark
        settler must not touch Kalshi positions."""
        tracker = PaperPositionTracker()
        ledger = PaperLedger(tmp_path)
        order = _order(platform=DataSource.KALSHI)
        _open_pm_position(tracker, order)
        settler = _settler(
            tracker, ledger, {order.client_order_id: order}, benchmark_price=105_000.0,
        )
        assert settler.settle_due() == 0

    def test_skips_when_strike_missing(self, tmp_path):
        tracker = PaperPositionTracker()
        ledger = PaperLedger(tmp_path)
        order = _order(strike=None)
        _open_pm_position(tracker, order)
        settler = _settler(
            tracker, ledger, {order.client_order_id: order}, benchmark_price=105_000.0,
        )
        assert settler.settle_due() == 0
        assert list(ledger.replay_settlements()) == []

    def test_skips_when_benchmark_price_unavailable(self, tmp_path):
        tracker = PaperPositionTracker()
        ledger = PaperLedger(tmp_path)
        order = _order()
        _open_pm_position(tracker, order)
        settler = _settler(
            tracker, ledger, {order.client_order_id: order}, benchmark_price=None,
        )
        assert settler.settle_due() == 0
        assert list(ledger.replay_settlements()) == []
