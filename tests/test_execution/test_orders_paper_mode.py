"""Tests for the dry_run_paper_mode flag on KalshiExecutor / OrderManager.

Round 8 Commit 2.  The flag is opt-in (default False) so the existing
test_integration.py suite — which exercises the optimistic auto-fill
behaviour of ``KalshiExecutor.refresh()`` in dry-run mode — keeps passing
unchanged.  When True, ``refresh()`` becomes a no-op so the paper
:class:`fill_simulator.FillSimulator` owns the FILLED transition.

Coverage:
* dry_run_paper_mode=False (default): regression-check that the existing
  refresh() instant-fill path still flips the order to FILLED.
* dry_run_paper_mode=True: submit() flips PENDING → PLACED, refresh() is
  a no-op (state stays PLACED, no average_fill_price set).
* OrderManager forwards the flag correctly to its KalshiExecutor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.execution.orders import (
    KalshiExecutor,
    Order,
    OrderManager,
    OrderState,
)
from btc_pm_arb.models import (
    ArbitrageSignal,
    DataSource,
    ProbabilityQuote,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=14)


def _arb_signal() -> ArbitrageSignal:
    pm_quote = ProbabilityQuote(
        source=DataSource.KALSHI,
        contract_id="pm-btc-100000",
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=0.40,
        ask_prob=0.44,
        mid_prob=0.42,
        settlement_type="kalshi_rti",
        timestamp=_NOW,
    )
    opt_quote = ProbabilityQuote(
        source=DataSource.DERIBIT,
        contract_id="pm-btc-100000",
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=0.56,
        ask_prob=0.60,
        mid_prob=0.58,
        settlement_type="deribit_twap",
        timestamp=_NOW,
    )
    return ArbitrageSignal(
        options_quote=opt_quote,
        pm_quote=pm_quote,
        raw_edge=0.16,
        adjusted_edge=0.12,
        trade_side="buy_yes",
        confidence=0.75,
        timestamp=_NOW,
    )


def _make_order_for_kalshi() -> Order:
    """Construct a minimal Order suitable for direct executor refresh tests."""
    return Order(
        client_order_id="co-test-1",
        signal=_arb_signal(),
        platform=DataSource.KALSHI,
        contract_id="pm-btc-100000",
        side="yes",
        size_usd=200.0,
        limit_price=0.44,
    )


# ── Default behaviour preserved (regression check) ────────────────────────────


@pytest.mark.asyncio
async def test_default_kalshi_executor_refresh_auto_fills_in_dry_run():
    """Default ``dry_run_paper_mode=False`` keeps the optimistic auto-fill on
    refresh() — this is the behaviour the existing test_integration.py tests
    rely on.  Regression check.
    """
    exec_ = KalshiExecutor(dry_run=True)   # default flag
    order = _make_order_for_kalshi()
    await exec_.submit(order)
    assert order.state == OrderState.PLACED

    await exec_.refresh(order)
    assert order.state == OrderState.FILLED
    assert order.filled_size == pytest.approx(200.0)
    assert order.average_fill_price == pytest.approx(0.44)
    await exec_.aclose()


@pytest.mark.asyncio
async def test_default_order_manager_refresh_all_auto_fills():
    """OrderManager(dry_run=True) without paper-mode flag: refresh_all fills."""
    mgr = OrderManager(dry_run=True)
    order = await mgr.place(_arb_signal(), size_usd=200.0)
    assert order is not None
    assert order.state == OrderState.PLACED

    await mgr.refresh_all()
    assert order.state == OrderState.FILLED
    await mgr.aclose()


# ── Paper-mode behaviour ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paper_mode_refresh_is_noop():
    """``dry_run_paper_mode=True``: submit() still flips to PLACED, but
    refresh() does not transition to FILLED (the paper FillSimulator owns
    that transition)."""
    exec_ = KalshiExecutor(dry_run=True, dry_run_paper_mode=True)
    order = _make_order_for_kalshi()
    await exec_.submit(order)
    assert order.state == OrderState.PLACED
    assert order.platform_order_id is not None   # submit still wires this

    await exec_.refresh(order)
    # Refresh is a no-op — state stays PLACED, filled_size untouched
    assert order.state == OrderState.PLACED
    assert order.filled_size == 0.0
    assert order.average_fill_price is None
    await exec_.aclose()


@pytest.mark.asyncio
async def test_paper_mode_repeated_refresh_is_idempotent():
    """Calling refresh() multiple times in paper mode does not change state."""
    exec_ = KalshiExecutor(dry_run=True, dry_run_paper_mode=True)
    order = _make_order_for_kalshi()
    await exec_.submit(order)

    await exec_.refresh(order)
    await exec_.refresh(order)
    await exec_.refresh(order)
    assert order.state == OrderState.PLACED
    assert order.filled_size == 0.0
    await exec_.aclose()


@pytest.mark.asyncio
async def test_paper_mode_via_order_manager_refresh_all_does_not_fill():
    """OrderManager(dry_run=True, dry_run_paper_mode=True) routes to the
    paper-mode KalshiExecutor — refresh_all() does not auto-fill."""
    mgr = OrderManager(dry_run=True, dry_run_paper_mode=True)
    order = await mgr.place(_arb_signal(), size_usd=200.0)
    assert order is not None
    assert order.state == OrderState.PLACED

    await mgr.refresh_all()
    assert order.state == OrderState.PLACED
    assert order.filled_size == 0.0
    # filled_orders() should be empty since nothing transitioned to FILLED
    assert mgr.filled_orders() == []
    await mgr.aclose()


# ── Flag forwarding ──────────────────────────────────────────────────────────


def test_order_manager_forwards_paper_mode_flag_to_executor():
    """OrderManager.__init__ correctly forwards dry_run_paper_mode to its
    internal KalshiExecutor."""
    mgr_default = OrderManager(dry_run=True)
    assert mgr_default._dry_run_paper_mode is False
    assert mgr_default._kalshi._dry_run_paper_mode is False

    mgr_paper = OrderManager(dry_run=True, dry_run_paper_mode=True)
    assert mgr_paper._dry_run_paper_mode is True
    assert mgr_paper._kalshi._dry_run_paper_mode is True


def test_kalshi_executor_paper_mode_flag_storage():
    """The flag round-trips through __init__ — defensive surface check."""
    ex_default = KalshiExecutor(dry_run=True)
    assert ex_default._dry_run_paper_mode is False

    ex_paper = KalshiExecutor(dry_run=True, dry_run_paper_mode=True)
    assert ex_paper._dry_run_paper_mode is True
