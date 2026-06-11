"""Integration tests — simulate the full pipeline end-to-end.

Scenarios covered
-----------------
1. Full pipeline: synthetic Deribit ticks → vol surface → digital price →
   cache update → PM tick matched → edge detected → signal passes filter →
   order placed (mocked) → position tracked → settlement recorded.

2. Dry run mode: OrderManager logs but does not call executor submit().

3. Risk limit rejection: position at cap → RiskManager.check() returns deny.

4. Signal below edge threshold: SignalFilter rejects → OrderManager never called.

5. Settlement outcome recording: settle() → SettlementRecord.outcome correct.

6. Graceful shutdown: stop_event cancels the scan task without errors.

7. Position P&L: unrealized and realized P&L computed correctly.

8. Order deduplication: placing the same signal twice emits only one order.

All network I/O (Kalshi REST, Polymarket CLOB) is mocked; no real credentials
or connections are required.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from btc_pm_arb.execution.orders import (
    KalshiExecutor,
    Order,
    OrderManager,
    OrderState,
    PolymarketExecutor,
)
from btc_pm_arb.execution.positions import PositionTracker
from btc_pm_arb.execution.risk import RiskConfig, RiskDecision, RiskManager
from btc_pm_arb.execution.settlement import Outcome, SettlementMonitor
from btc_pm_arb.models import (
    ArbitrageSignal,
    DataSource,
    OptionTick,
    OptionType,
    PredictionMarketTick,
    ProbabilityQuote,
)
from btc_pm_arb.pricing.cache import CacheEntry, ProbabilityCache
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.signals.confidence import ConfidenceScorer
from btc_pm_arb.signals.edge import EdgeCalculator
from btc_pm_arb.signals.filters import FilterConfig, SignalFilter
from btc_pm_arb.signals.matcher import ContractMatcher

# ── Shared fixtures / helpers ─────────────────────────────────────────────────

# _NOW stays wall-clock here (unlike test_signals/test_filters.py) because
# the vol-surface tests below flow through VolSurface/DigitalPricer, which
# read datetime.now() directly with no injection seam and need _EXPIRY in
# the real future.  The SignalFilter calls, however, inject clock=lambda:
# _NOW so the 300 s stale-data gate measures fixture age against the same
# instant the fixtures were stamped with -- otherwise those assertions
# depend on how much suite time elapsed since module import (the mechanism
# behind the 2026-06-10 test_fresh_data_passes flake).
_NOW = datetime.now(timezone.utc)
_EXPIRY = _NOW + timedelta(days=14)
_UNDERLYING = 100_000.0


def _option_tick(
    strike: float,
    opt_type: OptionType = OptionType.CALL,
    mark_iv: float = 65.0,
    bid_iv: float = 63.0,
    ask_iv: float = 67.0,
) -> OptionTick:
    return OptionTick(
        instrument_name=f"BTC-14APR26-{int(strike)}-{'C' if opt_type == OptionType.CALL else 'P'}",
        strike=strike,
        expiry=_EXPIRY,
        option_type=opt_type,
        bid=0.020,
        ask=0.025,
        mark_price=0.022,
        bid_iv=bid_iv,
        ask_iv=ask_iv,
        mark_iv=mark_iv,
        underlying_price=_UNDERLYING,
        index_price=_UNDERLYING,
        timestamp=_NOW,
    )


def _pm_tick(
    strike: float = 100_000.0,
    yes_bid: float = 0.40,
    yes_ask: float = 0.44,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=DataSource.KALSHI,
        contract_id=f"pm-btc-{int(strike)}",
        question=f"BTC above ${int(strike):,}?",
        strike=strike,
        expiry=_EXPIRY,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        # Realistic one-level depth so the require_nonempty_book gate passes.
        order_book_yes=[(yes_ask, 500.0)],
        order_book_no=[(round(1.0 - yes_bid, 4), 500.0)],
        timestamp=_NOW,
    )


def _arb_signal(
    yes_bid: float = 0.40,
    yes_ask: float = 0.44,
    options_bid: float = 0.56,
    options_ask: float = 0.60,
    confidence: float = 0.75,
) -> ArbitrageSignal:
    pm_quote = ProbabilityQuote(
        source=DataSource.KALSHI,
        contract_id="pm-btc-100000",
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=yes_bid,
        ask_prob=yes_ask,
        mid_prob=(yes_bid + yes_ask) / 2,
        settlement_type="kalshi_rti",
        timestamp=_NOW,
    )
    opt_quote = ProbabilityQuote(
        source=DataSource.DERIBIT,
        contract_id="pm-btc-100000",
        strike=100_000.0,
        expiry=_EXPIRY,
        bid_prob=options_bid,
        ask_prob=options_ask,
        mid_prob=(options_bid + options_ask) / 2,
        settlement_type="deribit_twap",
        timestamp=_NOW,
    )
    return ArbitrageSignal(
        options_quote=opt_quote,
        pm_quote=pm_quote,
        raw_edge=options_bid - yes_ask,
        adjusted_edge=options_bid - yes_ask,
        trade_side="buy_yes",
        confidence=confidence,
        timestamp=_NOW,
    )


def _populated_cache(
    strike: float = 100_000.0,
    bid: float = 0.56,
    ask: float = 0.60,
) -> ProbabilityCache:
    cache = ProbabilityCache()
    cache.update(
        strike=strike,
        expiry=_EXPIRY,
        bid_prob=bid,
        ask_prob=ask,
        mid_prob=(bid + ask) / 2,
        source=DataSource.DERIBIT,
        timestamp=_NOW,
    )
    return cache


# ── Scenario 1: Full pipeline (synthetic ticks → signal → order → settlement) ─

def test_vol_surface_ingests_ticks():
    surface = VolSurface()
    ticks = [
        _option_tick(90_000, mark_iv=75.0),
        _option_tick(95_000, mark_iv=70.0),
        _option_tick(100_000, mark_iv=65.0),
        _option_tick(105_000, mark_iv=62.0),
        _option_tick(110_000, mark_iv=60.0),
        _option_tick(115_000, mark_iv=60.0),
    ]
    dirty = surface.update(ticks)
    assert _EXPIRY in dirty
    smile = surface.get_smile(_EXPIRY)
    assert smile is not None
    assert smile.forward == pytest.approx(_UNDERLYING)


def test_cache_updated_from_surface():
    surface = VolSurface()
    ticks = [_option_tick(100_000 + i * 5_000, mark_iv=65.0 - i) for i in range(6)]
    surface.update(ticks)

    cache = ProbabilityCache()
    from btc_pm_arb.pricing.digital_pricer import DigitalPricer
    pricer = DigitalPricer()
    for t in ticks:
        price = pricer.price_from_surface(t.strike, t.expiry, surface)
        if price is not None:
            cache.update(
                strike=t.strike,
                expiry=t.expiry,
                bid_prob=price.bid,
                ask_prob=price.ask,
                mid_prob=price.mid,
                source=DataSource.DERIBIT,
                timestamp=_NOW,
            )

    entry = cache.get(100_000.0, _EXPIRY)
    assert entry is not None
    assert 0.0 < entry.mid_prob < 1.0


def test_matcher_links_pm_to_options():
    cache = _populated_cache()
    matcher = ContractMatcher()
    tick = _pm_tick(yes_bid=0.40, yes_ask=0.44)
    result = matcher.match(tick, cache)
    assert result is not None
    assert result.match_quality == pytest.approx(1.0)


def test_edge_calculator_detects_arb():
    cache = _populated_cache(bid=0.56, ask=0.60)
    matcher = ContractMatcher()
    calc = EdgeCalculator()

    tick = _pm_tick(yes_bid=0.40, yes_ask=0.44)
    match = matcher.match(tick, cache)
    assert match is not None

    edge = calc.compute(match)
    # edge_yes_conservative = 0.56 - 0.44 = 0.12 → clear arb
    assert edge.edge_yes_conservative == pytest.approx(0.12, abs=1e-9)
    assert edge.best_side == "buy_yes"


def test_signal_filter_passes_clear_arb():
    cache = _populated_cache(bid=0.56, ask=0.60)
    matcher = ContractMatcher()
    calc = EdgeCalculator()
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.05))

    tick = _pm_tick(yes_bid=0.40, yes_ask=0.44)
    match = matcher.match(tick, cache)
    edge = calc.compute(match)
    signals = filt.filter([edge], clock=lambda: _NOW)
    assert len(signals) == 1
    assert signals[0].trade_side == "buy_yes"


@pytest.mark.asyncio
async def test_full_pipeline_order_placed():
    """Synthetic ticks → matcher → edge → filter → order placed (dry run)."""
    cache = _populated_cache(bid=0.56, ask=0.60)
    matcher = ContractMatcher()
    calc = EdgeCalculator()
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.05))
    tracker = PositionTracker()
    risk = RiskManager()
    order_mgr = OrderManager(dry_run=True)

    tick = _pm_tick(yes_bid=0.40, yes_ask=0.44)
    match = matcher.match(tick, cache)
    edge = calc.compute(match)
    signals = filt.filter([edge], clock=lambda: _NOW)
    assert signals

    sig = signals[0].model_copy(update={"confidence": 0.80})
    proposed = risk.size_for_signal(sig, 200.0, tracker)
    decision = risk.check(sig, proposed, tracker)
    assert decision.allow

    order = await order_mgr.place(sig, size_usd=proposed)
    assert order is not None
    assert order.state in {OrderState.PLACED, OrderState.FILLED}

    await order_mgr.aclose()


@pytest.mark.asyncio
async def test_full_pipeline_position_and_settlement():
    """Order fill → position recorded → settlement → outcome WIN."""
    tracker = PositionTracker()
    order_mgr = OrderManager(dry_run=True)
    monitor = SettlementMonitor(tracker)

    sig = _arb_signal(confidence=0.80)
    order = await order_mgr.place(sig, size_usd=200.0)
    assert order is not None

    # Dry-run executor immediately fills
    await order_mgr.refresh_all()
    filled = [o for o in order_mgr.filled_orders()]
    assert filled

    for o in filled:
        pos = tracker.record_fill(o)
        assert pos is not None
        monitor.track(
            contract_id=o.contract_id,
            platform=o.platform,
            expiry=_EXPIRY,
            theoretical_edge=sig.adjusted_edge,
            side=o.side,
            entry_price=o.average_fill_price or o.limit_price,
            size_usd=o.filled_size,
        )

    # Simulate YES resolution (contract settles at 1.0)
    record = monitor.record_settlement("pm-btc-100000", DataSource.KALSHI, settlement_price=1.0)
    assert record is not None
    assert record.outcome == Outcome.WIN

    summary = monitor.performance_summary()
    assert summary["wins"] == 1
    assert summary["total_realized_pnl"] > 0

    await order_mgr.aclose()


# ── Scenario 2: Dry run mode ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_does_not_call_real_executor():
    """In dry-run mode, KalshiExecutor should NOT make HTTP calls."""
    mgr = OrderManager(dry_run=True)
    sig = _arb_signal()

    with patch.object(mgr._kalshi, "_client") as mock_client:
        await mgr.place(sig, size_usd=100.0)
        mock_client.post.assert_not_called()

    await mgr.aclose()


@pytest.mark.asyncio
async def test_dry_run_order_transitions_to_placed():
    mgr = OrderManager(dry_run=True)
    sig = _arb_signal()
    order = await mgr.place(sig, size_usd=150.0)
    assert order is not None
    assert order.state in {OrderState.PLACED, OrderState.FILLED}
    assert order.platform_order_id is not None
    await mgr.aclose()


# ── Scenario 3: Risk limit rejection ─────────────────────────────────────────

def test_risk_rejects_when_position_at_cap():
    tracker = PositionTracker()
    risk = RiskManager(RiskConfig(max_position_per_contract_usd=500.0))
    sig = _arb_signal()

    # Fill the cap
    tracker._positions[(DataSource.KALSHI, "pm-btc-100000")] = _mock_position(notional=500.0)

    decision = risk.check(sig, proposed_size_usd=100.0, tracker=tracker)
    assert not decision.allow
    assert "max_per_contract" in decision.reason or "position" in decision.reason


def test_risk_rejects_when_total_exposure_exceeded():
    tracker = PositionTracker()
    risk = RiskManager(RiskConfig(max_total_exposure_usd=1_000.0))
    sig = _arb_signal()

    # Simulate $900 in existing positions
    tracker._positions[(DataSource.POLYMARKET, "other-contract")] = _mock_position(notional=900.0)

    decision = risk.check(sig, proposed_size_usd=200.0, tracker=tracker)
    assert not decision.allow
    assert "total_exposure" in decision.reason


def test_risk_rejects_low_confidence():
    tracker = PositionTracker()
    risk = RiskManager(RiskConfig(min_confidence=0.60))
    sig = _arb_signal(confidence=0.35)
    decision = risk.check(sig, proposed_size_usd=100.0, tracker=tracker)
    assert not decision.allow
    assert "confidence" in decision.reason


def test_risk_rejects_too_many_open_positions():
    tracker = PositionTracker()
    risk = RiskManager(RiskConfig(max_open_positions=2))
    sig = _arb_signal()

    for i in range(2):
        tracker._positions[(DataSource.KALSHI, f"other-{i}")] = _mock_position(notional=100.0)

    decision = risk.check(sig, proposed_size_usd=100.0, tracker=tracker)
    assert not decision.allow
    assert "open_positions" in decision.reason


def test_risk_approves_clean_signal():
    tracker = PositionTracker()
    risk = RiskManager()
    sig = _arb_signal(confidence=0.80)
    decision = risk.check(sig, proposed_size_usd=100.0, tracker=tracker)
    assert decision.allow


def test_risk_size_for_signal_scales_by_confidence():
    tracker = PositionTracker()
    risk = RiskManager(RiskConfig(max_position_per_contract_usd=500.0))
    sig = _arb_signal(confidence=0.50)
    sized = risk.size_for_signal(sig, base_size_usd=200.0, tracker=tracker)
    assert sized == pytest.approx(100.0)   # 200 * 0.5


# ── Scenario 4: Signal below edge threshold ───────────────────────────────────

@pytest.mark.asyncio
async def test_signal_below_edge_threshold_not_placed():
    cache = _populated_cache(bid=0.46, ask=0.50)
    matcher = ContractMatcher()
    calc = EdgeCalculator()
    filt = SignalFilter(FilterConfig(min_conservative_edge=0.05))
    order_mgr = OrderManager(dry_run=True)

    tick = _pm_tick(yes_bid=0.43, yes_ask=0.47)
    match = matcher.match(tick, cache)
    assert match is not None

    edge = calc.compute(match)
    signals = filt.filter([edge], clock=lambda: _NOW)
    # edge_yes_conservative = 0.46 - 0.47 = -0.01 → no positive edge
    assert signals == []
    # No orders should be in the manager
    assert order_mgr.all_orders() == []
    await order_mgr.aclose()


# ── Scenario 5: Settlement outcome ───────────────────────────────────────────

def test_settlement_win_when_yes_resolves_true():
    tracker = PositionTracker()
    monitor = SettlementMonitor(tracker)
    monitor.track("contract-A", DataSource.KALSHI, _EXPIRY, 0.12, "yes", 0.42, 100.0)
    record = monitor.record_settlement("contract-A", DataSource.KALSHI, settlement_price=1.0)
    assert record is not None
    assert record.outcome == Outcome.WIN


def test_settlement_loss_when_yes_resolves_false():
    tracker = PositionTracker()
    monitor = SettlementMonitor(tracker)
    monitor.track("contract-B", DataSource.KALSHI, _EXPIRY, 0.12, "yes", 0.42, 100.0)
    record = monitor.record_settlement("contract-B", DataSource.KALSHI, settlement_price=0.0)
    assert record is not None
    assert record.outcome == Outcome.LOSS


def test_settlement_no_side_win_when_yes_resolves_false():
    """A NO position wins when YES resolves False."""
    tracker = PositionTracker()
    monitor = SettlementMonitor(tracker)
    monitor.track("contract-C", DataSource.KALSHI, _EXPIRY, 0.08, "no", 0.55, 100.0)
    record = monitor.record_settlement("contract-C", DataSource.KALSHI, settlement_price=0.0)
    assert record is not None
    assert record.outcome == Outcome.WIN


def test_settlement_unknown_contract_returns_none():
    tracker = PositionTracker()
    monitor = SettlementMonitor(tracker)
    result = monitor.record_settlement("nonexistent", DataSource.KALSHI, 1.0)
    assert result is None


def test_settlement_performance_summary():
    tracker = PositionTracker()
    monitor = SettlementMonitor(tracker)
    monitor.track("c1", DataSource.KALSHI, _EXPIRY, 0.10, "yes", 0.40, 100.0)
    monitor.track("c2", DataSource.KALSHI, _EXPIRY, 0.08, "yes", 0.45, 100.0)
    monitor.record_settlement("c1", DataSource.KALSHI, 1.0)
    monitor.record_settlement("c2", DataSource.KALSHI, 0.0)
    summary = monitor.performance_summary()
    assert summary["total_settled"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate"] == pytest.approx(0.5)


# ── Scenario 6: Graceful shutdown ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settlement_monitor_stops_on_event():
    """SettlementMonitor.run() exits cleanly when stop_event is set."""
    tracker = PositionTracker()
    monitor = SettlementMonitor(tracker)
    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(monitor.run(stop), _stop_soon())
    # If we reach here without hanging, the monitor stopped correctly


# ── Scenario 7: Position P&L ──────────────────────────────────────────────────

def test_position_unrealized_pnl():
    tracker = PositionTracker()
    order = _make_filled_order(fill_price=0.40, size=200.0)
    pos = tracker.record_fill(order)
    assert pos is not None

    tracker.update_mid("pm-btc-100000", mid_price=0.50, platform=DataSource.KALSHI)
    assert pos.unrealized_pnl == pytest.approx((0.50 - 0.40) * 200.0)


def test_position_realized_pnl_after_settlement():
    tracker = PositionTracker()
    order = _make_filled_order(fill_price=0.40, size=200.0)
    tracker.record_fill(order)

    settled = tracker.settle("pm-btc-100000", settlement_price=1.0, platform=DataSource.KALSHI)
    assert settled
    pos = settled[0]
    assert pos.closed
    assert pos.realized_pnl == pytest.approx((1.0 - 0.40) * 200.0)


def test_position_total_exposure():
    tracker = PositionTracker()
    o1 = _make_filled_order(fill_price=0.40, size=200.0, contract="pm-btc-100000")
    o2 = _make_filled_order(fill_price=0.55, size=150.0, contract="pm-btc-105000")
    tracker.record_fill(o1)
    tracker.record_fill(o2)
    assert tracker.total_exposure_usd() == pytest.approx(350.0)


# ── Scenario 8: Order deduplication ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_deduplication():
    """Placing the same signal twice should produce only one order."""
    mgr = OrderManager(dry_run=True)
    sig = _arb_signal()
    o1 = await mgr.place(sig, size_usd=100.0)
    o2 = await mgr.place(sig, size_usd=100.0)   # same fingerprint
    assert o1 is not None
    assert o2 is None
    assert len(mgr.all_orders()) == 1
    await mgr.aclose()


# ── Kalshi auth header tests ──────────────────────────────────────────────────

def test_kalshi_cents_conversion():
    exec_ = KalshiExecutor(dry_run=True)
    assert exec_._to_cents(0.42) == 42
    assert exec_._to_cents(0.999) == 99   # capped at 99
    assert exec_._to_cents(0.001) == 1    # floor at 1
    assert exec_._to_cents(0.506) == 51   # round up


def test_kalshi_apply_fill_dollars_field():
    """_apply_kalshi_fill handles _dollars field (float cents / 100)."""
    order = _make_filled_order(fill_price=0.0, size=0.0)
    data = {
        "status": "executed",
        "filled_count": 100.0,
        "avg_price_dollars": 42,   # cents, not dollars (Kalshi naming is confusing)
    }
    KalshiExecutor._apply_kalshi_fill(order, data)
    assert order.state == OrderState.FILLED
    assert order.filled_size == pytest.approx(100.0)
    assert order.average_fill_price == pytest.approx(0.42)


def test_kalshi_apply_fill_fp_field():
    """_apply_kalshi_fill handles _fp (fixed-point, 10^-4 cents → /1_000_000)."""
    order = _make_filled_order(fill_price=0.0, size=0.0)
    data = {
        "status": "executed",
        "filled_count": 50,
        "avg_price_fp": 420_000,   # = 0.42 in [0,1]
    }
    KalshiExecutor._apply_kalshi_fill(order, data)
    assert order.average_fill_price == pytest.approx(0.42)


# ── Confidence scorer integration ─────────────────────────────────────────────

def test_confidence_scorer_produces_score():
    """ConfidenceScorer integrates with a real EdgeResult (no surface)."""
    cache = _populated_cache(bid=0.56, ask=0.58)
    matcher = ContractMatcher()
    calc = EdgeCalculator()
    scorer = ConfidenceScorer()

    tick = _pm_tick(yes_bid=0.40, yes_ask=0.44)
    match = matcher.match(tick, cache)
    assert match is not None
    edge = calc.compute(match)
    score = scorer.score(edge)
    assert 0.0 <= score <= 1.0


# ── Private helpers ───────────────────────────────────────────────────────────

def _mock_position(notional: float, contract: str = "pm-btc-100000"):
    from btc_pm_arb.execution.positions import Position
    pos = Position(
        platform=DataSource.KALSHI,
        contract_id=contract,
        side="yes",
        filled_size=notional,
        entry_price=0.45,
    )
    return pos


def _make_filled_order(
    fill_price: float,
    size: float,
    contract: str = "pm-btc-100000",
) -> Order:
    sig = _arb_signal()
    order = Order(
        client_order_id="test-order-id",
        signal=sig,
        platform=DataSource.KALSHI,
        contract_id=contract,
        side="yes",
        size_usd=size,
        limit_price=fill_price,
        state=OrderState.FILLED,
        filled_size=size,
        average_fill_price=fill_price,
    )
    return order
