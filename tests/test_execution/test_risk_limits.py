"""Risk-limit layer tests (risk-limit goal, Phase 3).

Three layers of coverage:

1. ``check_risk`` pure-function units: each cap breach returns
   ``(False, reason)`` with the right reason, within-cap returns
   ``(True, "")``, boundary semantics (exactly-at-cap allowed for
   exposure, exactly-at-loss trips the daily brake), and first-breach
   precedence.

2. ``build_portfolio_state`` units against real JSONL ledgers: run_id
   scoping (other-run AND None-tagged/legacy records excluded), fill
   accumulation with the tracker's skip rules, settlement closing a
   triple out of exposure, and the today-only daily P&L window.

3. Integration through ``Agent.run_scan_pipeline``: an intent that
   passes ALL edge/confidence gates but breaches each cap is blocked
   with the correct reason and writes exactly one risk_block record (no
   order, no fill, no position); within-cap intents pass untouched;
   cross-run ledger records do not inflate any cap (the prior
   None-tagged-record misread, pinned as a regression test).

Agent seeding mirrors test_paper_pipeline.py: paper_ledger_dir pointed
at tmp_path, feed health freshened, cache populated so the crafted
KXBTC tick clears every signal gate (model 0.55 vs ask 0.42).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from btc_pm_arb.clock import SimulatedClock
from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
    PaperRiskBlockRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.execution.risk_limits import (
    PortfolioState,
    RiskIntent,
    RiskLimits,
    build_portfolio_state,
    check_risk,
)
from btc_pm_arb.main import Agent
from btc_pm_arb.models import DataSource, PredictionMarketTick

_CONTRACT = "KXBTC-26JUN30-B100000"
_RUN = "risk-test-run"


# -- Helpers -------------------------------------------------------------------


def _limits(
    per_market: float = 500.0,
    global_exposure: float = 5_000.0,
    daily_loss: float = 500.0,
) -> RiskLimits:
    # Explicit kwargs beat env/.env in pydantic-settings -- deterministic
    # regardless of the host environment.
    return RiskLimits(
        max_position_per_market=per_market,
        max_global_exposure=global_exposure,
        max_daily_loss=daily_loss,
    )


def _intent(size_usd: float = 200.0) -> RiskIntent:
    return RiskIntent(
        platform=DataSource.KALSHI,
        contract_id=_CONTRACT,
        side="yes",
        size_usd=size_usd,
    )


def _order_record(
    *,
    client_order_id: str,
    contract_id: str = _CONTRACT,
    platform: DataSource = DataSource.KALSHI,
    side: str = "yes",
    size_usd: float = 200.0,
    expiry: datetime | None = None,
) -> PaperOrderRecord:
    return PaperOrderRecord(
        client_order_id=client_order_id,
        signal_fingerprint=f"fp-{client_order_id}",
        created_at=datetime.now(timezone.utc),
        platform=platform,
        contract_id=contract_id,
        side=side,  # type: ignore[arg-type]
        size_usd=size_usd,
        limit_price=0.42,
        raw_edge=0.10,
        adjusted_edge=0.08,
        confidence=0.7,
        vol_regime="low",
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        expiry=expiry or (datetime.now(timezone.utc) + timedelta(days=7)),
    )


def _fill_record(
    *,
    client_order_id: str,
    fill_size_usd: float,
    fill_price: float | None = 0.42,
    fill_outcome: str = "full",
) -> PaperFillRecord:
    return PaperFillRecord(
        client_order_id=client_order_id,
        filled_at=datetime.now(timezone.utc),
        fill_price=fill_price,
        fill_size_usd=fill_size_usd,
        fill_outcome=fill_outcome,  # type: ignore[arg-type]
        simulator_reason="test",
    )


def _settlement_record(
    *,
    client_order_id: str,
    contract_id: str = _CONTRACT,
    platform: DataSource = DataSource.KALSHI,
    side: str = "yes",
    realized_pnl: float,
    settled_at: datetime,
) -> PaperSettlementRecord:
    return PaperSettlementRecord(
        client_order_id=client_order_id,
        contract_id=contract_id,
        platform=platform,
        side=side,  # type: ignore[arg-type]
        settled_at=settled_at,
        settlement_price=1.0,
        payout_price=1.0,
        entry_price=0.42,
        size_usd=200.0,
        realized_pnl=realized_pnl,
        outcome="win" if realized_pnl >= 0 else "loss",
        theoretical_edge=0.10,
        expiry=settled_at,
    )


def _seed_open_position(
    ledger: PaperLedger,
    *,
    client_order_id: str,
    contract_id: str = _CONTRACT,
    platform: DataSource = DataSource.KALSHI,
    side: str = "yes",
    filled_usd: float = 200.0,
) -> None:
    """Append a matched order+fill pair (one open position triple)."""
    ledger.append_order(
        _order_record(
            client_order_id=client_order_id,
            contract_id=contract_id,
            platform=platform,
            side=side,
            size_usd=filled_usd,
        )
    )
    ledger.append_fill(
        _fill_record(client_order_id=client_order_id, fill_size_usd=filled_usd)
    )


def _make_pm_tick(
    *,
    expiry: datetime,
    contract_id: str = _CONTRACT,
    timestamp: datetime | None = None,
    yes_bid: float = 0.40,
    yes_ask: float = 0.42,
    no_bid: float = 0.58,
    no_ask: float = 0.60,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=DataSource.KALSHI,
        contract_id=contract_id,
        question="BTC above $100,000 by Jun 30?",
        strike=100_000.0,
        expiry=expiry,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        order_book_yes=[(yes_ask, 500.0)],
        order_book_no=[(no_ask, 500.0)],
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def _seed_agent(
    monkeypatch,
    tmp_path: Path,
    *,
    run_id: str = _RUN,
    clock: SimulatedClock | None = None,
) -> tuple[Agent, datetime]:
    """Agent on a tmp ledger dir with a cache that makes the tick pass all gates.

    Pass an anchored replay ``clock`` to pin 'now' (and the daily-loss
    day bucket) to sim-time -- removes any UTC-midnight flake window.
    """
    monkeypatch.setattr(
        "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
    )
    agent = Agent(dry_run=True, run_id=run_id, clock=clock)
    agent.feed_health.record_tick(DataSource.DERIBIT)
    agent.feed_health.record_tick(DataSource.KALSHI)
    expiry = agent.clock.now() + timedelta(days=7)
    agent.cache.update(
        strike=100_000.0,
        expiry=expiry,
        bid_prob=0.55,
        ask_prob=0.55,
        mid_prob=0.55,
        source=DataSource.DERIBIT,
    )
    return agent, expiry


async def _scan_once(
    agent: Agent,
    expiry: datetime,
    *,
    timestamp: datetime | None = None,
    yes_ask: float = 0.42,
    no_ask: float = 0.60,
) -> None:
    tick = _make_pm_tick(
        expiry=expiry,
        timestamp=timestamp,
        yes_bid=yes_ask - 0.02,
        yes_ask=yes_ask,
        no_bid=no_ask - 0.02,
        no_ask=no_ask,
    )
    agent.ingest_pm_tick(tick)
    await agent.run_scan_pipeline(agent.flush_pm_ticks())


# ==============================================================================
# 1. check_risk -- pure function
# ==============================================================================


def test_check_risk_within_all_caps_allows():
    state = PortfolioState(
        market_position_usd=100.0,
        global_exposure_usd=1_000.0,
        daily_realized_pnl_usd=-100.0,
    )
    allow, reason = check_risk(_intent(200.0), state, _limits())
    assert allow is True
    assert reason == ""


def test_check_risk_blocks_per_market_breach():
    state = PortfolioState(
        market_position_usd=400.0,
        global_exposure_usd=400.0,
        daily_realized_pnl_usd=0.0,
    )
    allow, reason = check_risk(_intent(200.0), state, _limits(per_market=500.0))
    assert allow is False
    assert "max_position_per_market" in reason
    assert _CONTRACT in reason


def test_check_risk_blocks_global_exposure_breach():
    state = PortfolioState(
        market_position_usd=0.0,
        global_exposure_usd=4_900.0,
        daily_realized_pnl_usd=0.0,
    )
    allow, reason = check_risk(
        _intent(200.0), state, _limits(global_exposure=5_000.0),
    )
    assert allow is False
    assert "max_global_exposure" in reason


def test_check_risk_blocks_daily_loss_breach():
    state = PortfolioState(
        market_position_usd=0.0,
        global_exposure_usd=0.0,
        daily_realized_pnl_usd=-600.0,
    )
    allow, reason = check_risk(_intent(200.0), state, _limits(daily_loss=500.0))
    assert allow is False
    assert "max_daily_loss" in reason


def test_check_risk_exactly_at_exposure_caps_allows():
    # Exposure caps block on strict ">": landing exactly on the cap is fine.
    state = PortfolioState(
        market_position_usd=300.0,
        global_exposure_usd=4_800.0,
        daily_realized_pnl_usd=0.0,
    )
    allow, reason = check_risk(
        _intent(200.0),
        state,
        _limits(per_market=500.0, global_exposure=5_000.0),
    )
    assert allow is True
    assert reason == ""


def test_check_risk_exactly_at_daily_loss_blocks():
    # The daily brake trips on "<=": losing exactly the cap amount blocks.
    state = PortfolioState(
        market_position_usd=0.0,
        global_exposure_usd=0.0,
        daily_realized_pnl_usd=-500.0,
    )
    allow, reason = check_risk(_intent(200.0), state, _limits(daily_loss=500.0))
    assert allow is False
    assert "max_daily_loss" in reason


def test_check_risk_first_breach_wins_in_declaration_order():
    # All three breached -> per-market (declared first) names the reason.
    state = PortfolioState(
        market_position_usd=10_000.0,
        global_exposure_usd=10_000.0,
        daily_realized_pnl_usd=-10_000.0,
    )
    allow, reason = check_risk(_intent(200.0), state, _limits())
    assert allow is False
    assert "max_position_per_market" in reason


# ==============================================================================
# 2. build_portfolio_state -- run_id-scoped event-sourced reconstruction
# ==============================================================================


def _state(
    tmp_path: Path,
    *,
    run_id: str = _RUN,
    today: datetime | None = None,
) -> PortfolioState:
    reader = PaperLedger(tmp_path)
    now = today or datetime.now(timezone.utc)
    return build_portfolio_state(
        reader,
        run_id=run_id,
        platform=DataSource.KALSHI,
        contract_id=_CONTRACT,
        today=now.date(),
    )


def test_state_accumulates_run_scoped_fills(tmp_path: Path):
    ledger = PaperLedger(tmp_path, run_id=_RUN)
    _seed_open_position(ledger, client_order_id="co-1", filled_usd=200.0)
    _seed_open_position(ledger, client_order_id="co-2", filled_usd=150.0)
    # Same market, other side: still counts toward the market cap.
    _seed_open_position(
        ledger, client_order_id="co-3", side="no", filled_usd=100.0,
    )
    # Different market: global only.
    _seed_open_position(
        ledger,
        client_order_id="co-4",
        contract_id="KXBTC-26JUL31-B120000",
        filled_usd=300.0,
    )

    state = _state(tmp_path)
    assert state.market_position_usd == pytest.approx(450.0)
    assert state.global_exposure_usd == pytest.approx(750.0)
    assert state.daily_realized_pnl_usd == pytest.approx(0.0)


def test_state_excludes_other_run_and_none_tagged_records(tmp_path: Path):
    # Other-run records: stamped with a different run_id.
    other = PaperLedger(tmp_path, run_id="other-run")
    _seed_open_position(other, client_order_id="other-1", filled_usd=400.0)
    other.append_settlement(
        _settlement_record(
            client_order_id="other-1",
            contract_id="KXBTC-26JUL31-B120000",
            realized_pnl=-600.0,
            settled_at=datetime.now(timezone.utc),
        )
    )
    # Legacy/None-tagged records: a bare ledger stamps run_id="" -- the
    # same field value pre-stamp records deserialize to.
    legacy = PaperLedger(tmp_path)
    _seed_open_position(legacy, client_order_id="legacy-1", filled_usd=400.0)

    state = _state(tmp_path)
    assert state.market_position_usd == pytest.approx(0.0)
    assert state.global_exposure_usd == pytest.approx(0.0)
    assert state.daily_realized_pnl_usd == pytest.approx(0.0)


def test_state_applies_tracker_skip_rules(tmp_path: Path):
    ledger = PaperLedger(tmp_path, run_id=_RUN)
    ledger.append_order(_order_record(client_order_id="co-1"))
    # no_fill, None-price, and non-positive-size fills must not count.
    # The must-skip records carry POSITIVE sizes (except the <=0 rule,
    # which is zero by definition) so that counting any of them would
    # break the assertions below -- a 0-size record cannot discriminate.
    ledger.append_fill(
        _fill_record(
            client_order_id="co-1",
            fill_size_usd=300.0,
            fill_price=0.42,
            fill_outcome="no_fill",
        )
    )
    ledger.append_fill(
        _fill_record(
            client_order_id="co-1", fill_size_usd=250.0, fill_price=None,
        )
    )
    ledger.append_fill(
        _fill_record(client_order_id="co-1", fill_size_usd=0.0)
    )
    # Fill with no matching current-run order must not count either.
    ledger.append_fill(
        _fill_record(client_order_id="unknown-order", fill_size_usd=500.0)
    )
    # One real fill.
    ledger.append_fill(
        _fill_record(client_order_id="co-1", fill_size_usd=120.0)
    )

    state = _state(tmp_path)
    assert state.market_position_usd == pytest.approx(120.0)
    assert state.global_exposure_usd == pytest.approx(120.0)


def test_state_settlement_closes_triple_and_buckets_daily_pnl(tmp_path: Path):
    ledger = PaperLedger(tmp_path, run_id=_RUN)
    now = datetime.now(timezone.utc)
    _seed_open_position(ledger, client_order_id="co-1", filled_usd=400.0)
    # Settling the triple removes it from exposure; pnl counts today.
    ledger.append_settlement(
        _settlement_record(
            client_order_id="co-1", realized_pnl=-120.0, settled_at=now,
        )
    )
    # A settlement dated yesterday must not count toward today's pnl.
    ledger.append_settlement(
        _settlement_record(
            client_order_id="co-old",
            contract_id="KXBTC-26MAY31-B90000",
            realized_pnl=-999.0,
            settled_at=now - timedelta(days=1),
        )
    )

    state = _state(tmp_path, today=now)
    assert state.market_position_usd == pytest.approx(0.0)
    assert state.global_exposure_usd == pytest.approx(0.0)
    assert state.daily_realized_pnl_usd == pytest.approx(-120.0)


# ==============================================================================
# 3. Ledger round-trip for the new record kind
# ==============================================================================


def test_risk_block_record_stamped_and_round_trips(tmp_path: Path):
    ledger = PaperLedger(tmp_path, run_id="r1", mode="replay")
    ledger.append_risk_block(
        PaperRiskBlockRecord(
            timestamp=datetime.now(timezone.utc),
            platform=DataSource.KALSHI,
            contract_id=_CONTRACT,
            side="yes",
            size_usd=200.0,
            reason="test-reason",
            market_position_usd=400.0,
            global_exposure_usd=400.0,
            daily_realized_pnl_usd=0.0,
        )
    )
    fresh = PaperLedger(tmp_path)
    records = list(fresh.replay_risk_blocks())
    assert len(records) == 1
    rec = records[0]
    assert rec.kind == "risk_block"
    assert rec.run_id == "r1"
    assert rec.mode == "replay"
    assert rec.reason == "test-reason"
    assert rec.market_position_usd == pytest.approx(400.0)


# ==============================================================================
# 4. Integration through Agent.run_scan_pipeline
# ==============================================================================


def _assert_blocked(
    agent: Agent, *, reason_contains: str, n_preseeded_orders: int,
) -> PaperRiskBlockRecord:
    """Shared assertions for a blocked intent: one record, no order/fill."""
    blocks = list(agent.paper_ledger.replay_risk_blocks())
    assert len(blocks) == 1
    block = blocks[0]
    assert reason_contains in block.reason
    assert block.run_id == _RUN
    assert block.contract_id == _CONTRACT
    assert block.platform == DataSource.KALSHI
    assert block.side == "yes"
    assert block.size_usd == pytest.approx(200.0)

    # No order was built or recorded for the blocked intent.
    orders = list(agent.paper_ledger.replay_orders())
    assert len(orders) == n_preseeded_orders
    assert agent._funnel["paper_orders_placed"] == 0
    assert agent._funnel["paper_orders_risk_blocked"] == 1
    return block


@pytest.mark.asyncio
async def test_per_market_cap_blocks_passing_signal(monkeypatch, tmp_path: Path):
    # Pre-seed 400 USD of current-run open position on the SAME market.
    seed = PaperLedger(tmp_path, run_id=_RUN)
    _seed_open_position(seed, client_order_id="seed-1", filled_usd=400.0)

    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits(
        per_market=500.0, global_exposure=100_000.0, daily_loss=100_000.0,
    )
    await _scan_once(agent, expiry)

    block = _assert_blocked(
        agent, reason_contains="max_position_per_market", n_preseeded_orders=1,
    )
    # The record carries the state the decision was made against.
    assert block.market_position_usd == pytest.approx(400.0)
    assert block.global_exposure_usd == pytest.approx(400.0)
    # No fill, no new position beyond the rehydrated seed.
    assert len(list(agent.paper_ledger.replay_fills())) == 1   # the seed fill
    assert len(agent.paper_positions.open_positions()) == 1    # rehydrated seed


@pytest.mark.asyncio
async def test_global_exposure_cap_blocks_passing_signal(
    monkeypatch, tmp_path: Path,
):
    # 4900 USD of current-run exposure spread over OTHER markets.
    seed = PaperLedger(tmp_path, run_id=_RUN)
    _seed_open_position(
        seed,
        client_order_id="seed-1",
        contract_id="KXBTC-26JUL31-B120000",
        filled_usd=2_500.0,
    )
    _seed_open_position(
        seed,
        client_order_id="seed-2",
        contract_id="KXBTC-26AUG31-B130000",
        filled_usd=2_400.0,
    )

    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits(
        per_market=100_000.0, global_exposure=5_000.0, daily_loss=100_000.0,
    )
    await _scan_once(agent, expiry)

    block = _assert_blocked(
        agent, reason_contains="max_global_exposure", n_preseeded_orders=2,
    )
    assert block.market_position_usd == pytest.approx(0.0)
    assert block.global_exposure_usd == pytest.approx(4_900.0)


@pytest.mark.asyncio
async def test_daily_loss_cap_blocks_passing_signal(monkeypatch, tmp_path: Path):
    # Anchored sim clock: seed-settlement day and the agent's "today"
    # bucket come from the same fixed mid-day instant, so the test cannot
    # flake across a UTC-midnight rollover.
    anchor = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    # A current-run settlement realized today at -600 trips the 500 brake.
    seed = PaperLedger(tmp_path, run_id=_RUN)
    seed.append_settlement(
        _settlement_record(
            client_order_id="seed-1",
            contract_id="KXBTC-26MAY31-B90000",
            realized_pnl=-600.0,
            settled_at=anchor,
        )
    )

    agent, expiry = _seed_agent(
        monkeypatch,
        tmp_path,
        clock=SimulatedClock("replay", start=anchor),
    )
    agent.risk_limits = _limits(
        per_market=100_000.0, global_exposure=100_000.0, daily_loss=500.0,
    )
    await _scan_once(agent, expiry, timestamp=anchor)

    block = _assert_blocked(
        agent, reason_contains="max_daily_loss", n_preseeded_orders=0,
    )
    assert block.daily_realized_pnl_usd == pytest.approx(-600.0)


@pytest.mark.asyncio
async def test_within_cap_intent_passes_untouched(monkeypatch, tmp_path: Path):
    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits()   # PAPER defaults: 500 / 5000 / 500
    await _scan_once(agent, expiry)

    # The order went through exactly as the pre-risk-layer pipeline did.
    orders = list(agent.paper_ledger.replay_orders())
    assert len(orders) == 1
    assert orders[0].contract_id == _CONTRACT
    assert orders[0].side == "yes"
    assert orders[0].size_usd == pytest.approx(200.0)
    assert orders[0].limit_price == pytest.approx(0.42)
    fills = list(agent.paper_ledger.replay_fills())
    assert len(fills) == 1
    assert fills[0].fill_outcome == "full"
    assert len(agent.paper_positions.open_positions()) == 1

    # And the risk layer left no trace.
    assert list(agent.paper_ledger.replay_risk_blocks()) == []
    assert agent._funnel["paper_orders_risk_blocked"] == 0
    assert agent._funnel["paper_orders_placed"] == 1


@pytest.mark.asyncio
async def test_cross_run_records_do_not_inflate_any_cap(
    monkeypatch, tmp_path: Path,
):
    """The prior misread, pinned: None-tagged + other-run ledger records
    sharing the dir must not count toward any cap."""
    # Would breach per-market AND global AND daily loss if counted:
    other = PaperLedger(tmp_path, run_id="other-run")
    _seed_open_position(other, client_order_id="other-1", filled_usd=400.0)
    _seed_open_position(
        other,
        client_order_id="other-2",
        contract_id="KXBTC-26JUL31-B120000",
        filled_usd=9_000.0,
    )
    other.append_settlement(
        _settlement_record(
            client_order_id="other-3",
            contract_id="KXBTC-26MAY31-B90000",
            realized_pnl=-600.0,
            settled_at=datetime.now(timezone.utc),
        )
    )
    legacy = PaperLedger(tmp_path)   # stamps run_id="" (None-tagged shape)
    _seed_open_position(legacy, client_order_id="legacy-1", filled_usd=400.0)

    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits()   # 500 / 5000 / 500
    await _scan_once(agent, expiry)

    # Not blocked: the order placed and filled normally.
    assert list(agent.paper_ledger.replay_risk_blocks()) == []
    assert agent._funnel["paper_orders_risk_blocked"] == 0
    assert agent._funnel["paper_orders_placed"] == 1
    orders = list(agent.paper_ledger.replay_orders())
    # 3 pre-seeded (other-run + legacy) + 1 new.
    assert len(orders) == 4
    new = [o for o in orders if o.run_id == _RUN]
    assert len(new) == 1
    assert new[0].contract_id == _CONTRACT


@pytest.mark.asyncio
async def test_already_placed_signal_is_not_risk_checked(
    monkeypatch, tmp_path: Path,
):
    """The is_duplicate gate: a signal that already placed re-arrives on a
    later scan; even though the caps would now block it, place() dedupes
    it anyway -- so no risk_block record may be written for it."""
    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits()   # within caps -> places normally
    await _scan_once(agent, expiry)
    assert agent._funnel["paper_orders_placed"] == 1

    # Tighten the cap so the SAME signal would breach if re-evaluated:
    # the first order filled 200, and 200 + 200 > 300.
    agent.risk_limits = _limits(per_market=300.0)
    await _scan_once(agent, expiry)

    # No risk artifact, no second order: the duplicate never reached the
    # cap check, and place() deduped it.
    assert list(agent.paper_ledger.replay_risk_blocks()) == []
    assert agent._funnel["paper_orders_risk_blocked"] == 0
    assert agent._funnel["paper_orders_placed"] == 1
    assert len(list(agent.paper_ledger.replay_orders())) == 1


@pytest.mark.asyncio
async def test_edge_rejected_signal_never_reaches_risk_layer(
    monkeypatch, tmp_path: Path,
):
    """Ordering pin: the risk layer runs strictly AFTER the edge gates.
    A signal that fails the edge floor must be rejected by the filter --
    never cap-checked, never risk_block-recorded -- even when the seeded
    state breaches every cap."""
    seed = PaperLedger(tmp_path, run_id=_RUN)
    _seed_open_position(seed, client_order_id="seed-1", filled_usd=400.0)

    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits(per_market=500.0)   # 400 + 200 would breach
    # Model fair 0.55 vs yes_ask 0.56 / no_ask 0.46: both sides have
    # negative conservative edge -> rejected at the edge-economics gate.
    await _scan_once(agent, expiry, yes_ask=0.56, no_ask=0.46)

    assert agent._funnel["signals_rejected_filter"] == 1
    assert agent._funnel["signals_passed_filter"] == 0
    assert list(agent.paper_ledger.replay_risk_blocks()) == []
    assert agent._funnel["paper_orders_risk_blocked"] == 0
    assert agent._funnel["paper_orders_placed"] == 0
    assert len(list(agent.paper_ledger.replay_orders())) == 1   # seed only


@pytest.mark.asyncio
async def test_duplicate_ticks_in_one_scan_block_once(
    monkeypatch, tmp_path: Path,
):
    """The 5 s buffer can hold several ticks for one contract and
    batch_match does not dedupe -- a breached cap must still produce
    exactly ONE risk_block record for that intent within the scan."""
    seed = PaperLedger(tmp_path, run_id=_RUN)
    _seed_open_position(seed, client_order_id="seed-1", filled_usd=400.0)

    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits(
        per_market=500.0, global_exposure=100_000.0, daily_loss=100_000.0,
    )
    agent.ingest_pm_tick(_make_pm_tick(expiry=expiry))
    agent.ingest_pm_tick(_make_pm_tick(expiry=expiry))
    await agent.run_scan_pipeline(agent.flush_pm_ticks())

    assert len(list(agent.paper_ledger.replay_risk_blocks())) == 1
    assert agent._funnel["paper_orders_risk_blocked"] == 1
    assert agent._funnel["paper_orders_placed"] == 0


@pytest.mark.asyncio
async def test_persistent_breach_blocks_once_per_intent(
    monkeypatch, tmp_path: Path,
):
    """A breach that persists across scans blocks each re-arriving intent
    with exactly one record per intent (the fingerprint is never
    registered, so the signal re-evaluates -- and would trade again once
    headroom returns)."""
    seed = PaperLedger(tmp_path, run_id=_RUN)
    _seed_open_position(seed, client_order_id="seed-1", filled_usd=400.0)

    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    agent.risk_limits = _limits(
        per_market=500.0, global_exposure=100_000.0, daily_loss=100_000.0,
    )
    await _scan_once(agent, expiry)
    await _scan_once(agent, expiry)

    blocks = list(agent.paper_ledger.replay_risk_blocks())
    assert len(blocks) == 2
    assert agent._funnel["paper_orders_risk_blocked"] == 2
    assert agent._funnel["paper_orders_placed"] == 0
    assert len(list(agent.paper_ledger.replay_orders())) == 1   # seed only
