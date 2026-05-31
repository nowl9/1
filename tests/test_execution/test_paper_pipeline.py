"""End-to-end paper-trading pipeline test.

Round 8 Commit 3.  Exercises the full live wire:

  Agent.run_scan_pipeline (with a passing PM tick + populated cache)
    → OrderManager.place
    → PaperLedger.append_order
    → FillSimulator.evaluate + build_fill_record
    → PaperLedger.append_fill
    → PaperPositionTracker.record_fill

Then mark-to-market against a fresh tick at a higher mid:

  PaperPositionTracker.mark_to_market
    → asserts unrealized_pnl > 0 and last_mark_at bumped

Then settle via a manually-driven KalshiSettlementPoller against a
mocked HTTP response:

  poller.poll_once (with status=settled, result=yes)
    → PaperLedger.append_settlement
    → PaperPositionTracker.settle
    → asserts position closed, realized_pnl correct,
      signal_fired_skipped.paper_orders_filled == 1

Mocks the HTTP layer only — every other component runs real.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from btc_pm_arb.execution.paper_ledger import PaperLedger
from btc_pm_arb.execution.paper_settlement import KalshiSettlementPoller
from btc_pm_arb.main import Agent
from btc_pm_arb.models import DataSource, PredictionMarketTick


# ── Mock HTTP plumbing for the settlement poller ──────────────────────────────


class _MockResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _MockClient:
    """Minimal async HTTP client mapping ticker → market payload."""

    def __init__(self, by_ticker: dict[str, Any]) -> None:
        self._by_ticker = by_ticker
        self.calls: list[str] = []

    async def get(self, path: str, headers: dict[str, str] | None = None):
        self.calls.append(path)
        ticker = path.rsplit("/", 1)[-1]
        return _MockResponse(self._by_ticker.get(ticker, {"market": {"status": "open"}}))

    async def aclose(self) -> None:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_pm_tick(
    *,
    contract_id: str = "KXBTC-26JUN30-B100000",
    expiry: datetime,
    yes_bid: float = 0.40,
    yes_ask: float = 0.42,
    no_bid: float = 0.58,
    no_ask: float = 0.60,
    timestamp: datetime | None = None,
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
        # Realistic one-level depth so the require_nonempty_book gate passes.
        order_book_yes=[(yes_ask, 500.0)],
        order_book_no=[(no_ask, 500.0)],
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def _seed_agent(monkeypatch, tmp_path: Path) -> tuple[Agent, datetime]:
    """Build an Agent with paper_ledger pointed at tmp_path and a populated cache."""
    monkeypatch.setattr(
        "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
    )
    agent = Agent(dry_run=True)

    # Freshen the feed-health gate so signals don't get rejected as stale.
    agent.feed_health.record_tick(DataSource.DERIBIT)
    agent.feed_health.record_tick(DataSource.KALSHI)

    # 7d-out expiry passes the (1d ≤ T ≤ 90d) bounds filter.
    expiry = datetime.now(timezone.utc) + timedelta(days=7)
    agent.cache.update(
        strike=100_000.0,
        expiry=expiry,
        bid_prob=0.55,
        ask_prob=0.55,
        mid_prob=0.55,
        source=DataSource.DERIBIT,
    )
    return agent, expiry


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end test
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_paper_pipeline_place_simulate_mark_settle(
    monkeypatch, tmp_path: Path,
):
    """Walk a passing signal through place → fill → mark → settle and assert
    each layer's state is what the next layer reads from."""
    agent, expiry = _seed_agent(monkeypatch, tmp_path)

    # ── Step 1: passing signal → order placed → fill simulated → recorded ─
    tick = _make_pm_tick(
        expiry=expiry,
        yes_bid=0.40,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
    )
    agent.ingest_pm_tick(tick)
    pm_ticks = agent.flush_pm_ticks()
    await agent.run_scan_pipeline(pm_ticks)

    # Order JSONL has exactly one record
    order_records = list(agent.paper_ledger.replay_orders())
    assert len(order_records) == 1
    order_rec = order_records[0]
    assert order_rec.platform == DataSource.KALSHI
    assert order_rec.contract_id == "KXBTC-26JUN30-B100000"
    assert order_rec.side == "yes"
    assert order_rec.size_usd == pytest.approx(200.0)
    assert order_rec.limit_price == pytest.approx(0.42)   # = yes_ask

    # Fill JSONL has exactly one full-fill record
    fill_records = list(agent.paper_ledger.replay_fills())
    assert len(fill_records) == 1
    fill_rec = fill_records[0]
    assert fill_rec.client_order_id == order_rec.client_order_id
    assert fill_rec.fill_outcome == "full"
    assert fill_rec.fill_price == pytest.approx(0.42)
    assert fill_rec.fill_size_usd == pytest.approx(200.0)

    # In-memory paper position created with the right entry
    open_positions = agent.paper_positions.open_positions()
    assert len(open_positions) == 1
    pos = open_positions[0]
    assert pos.entry_price == pytest.approx(0.42)
    assert pos.filled_size_usd == pytest.approx(200.0)
    assert pos.side == "yes"
    assert pos.contract_id == "KXBTC-26JUN30-B100000"
    assert pos.current_mid is None      # M2M hasn't run yet on this position

    # Funnel counters recorded the placement and fill
    assert agent._funnel["paper_orders_placed"] == 1
    assert agent._funnel["paper_orders_filled"] == 1
    assert agent._funnel["paper_orders_no_fill"] == 0

    # ── Step 2: second tick at a higher mid → mark-to-market → unrealized > 0 ─
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    higher = _make_pm_tick(
        expiry=expiry,
        yes_bid=0.49,
        yes_ask=0.51,
        no_bid=0.49,
        no_ask=0.51,
        timestamp=later,
    )
    agent.paper_positions.mark_to_market([higher])

    # current_mid updated, unrealized_pnl positive (entry 0.42 → mid 0.50)
    assert pos.current_mid == pytest.approx(0.50)
    # (0.50 - 0.42) * 200 = 16.0
    assert pos.unrealized_pnl == pytest.approx(16.0)
    # last_mark_at bumped to the new tick's timestamp
    assert pos.last_mark_at == later

    # ── Step 3: settle via the poller against a mocked HTTP response ─────
    settlement_payload = {
        "market": {
            "ticker": "KXBTC-26JUN30-B100000",
            "status": "settled",
            "result": "yes",
        }
    }
    mock_client = _MockClient({"KXBTC-26JUN30-B100000": settlement_payload})

    # Construct a fresh poller against the agent's tracker + ledger and the
    # mock HTTP client.  Inject a clock at expiry + 2min so the polling
    # window logic accepts the position.
    settle_clock = expiry + timedelta(minutes=2)
    poller = KalshiSettlementPoller(
        tracker=agent.paper_positions,
        ledger=agent.paper_ledger,
        get_order_record=lambda cid: agent._paper_orders_by_id.get(cid),
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        key_path="/nonexistent.pem",   # never loaded — http_client injected
        key_id="test-key",
        http_client=mock_client,        # type: ignore[arg-type]
        clock=lambda: settle_clock,
    )

    n = await poller.poll_once()
    assert n == 1

    # Position closed
    assert pos.closed
    assert pos.settlement_price == pytest.approx(1.0)
    # Realized P&L: (1.0 - 0.42) * 200 = 116.0
    assert pos.realized_pnl == pytest.approx(116.0)

    # Settlement JSONL has exactly one record
    settlement_records = list(agent.paper_ledger.replay_settlements())
    assert len(settlement_records) == 1
    settle_rec = settlement_records[0]
    assert settle_rec.outcome == "win"
    assert settle_rec.client_order_id == order_rec.client_order_id
    assert settle_rec.realized_pnl == pytest.approx(116.0)
    assert settle_rec.theoretical_edge == pytest.approx(order_rec.adjusted_edge)

    # Funnel counters unchanged from step 1 (no new orders placed)
    assert agent._funnel["paper_orders_placed"] == 1
    assert agent._funnel["paper_orders_filled"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# Restart-replay smoke test — paper state survives Agent reconstruction
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_paper_state_survives_agent_restart(monkeypatch, tmp_path: Path):
    """Ledger persistence + replay-on-startup: place an order in agent A,
    discard agent A, construct agent B against the same dir, assert paper
    state is recovered.
    """
    # Agent A — places one paper order
    agent_a, expiry = _seed_agent(monkeypatch, tmp_path)
    tick = _make_pm_tick(expiry=expiry)
    agent_a.ingest_pm_tick(tick)
    await agent_a.run_scan_pipeline(agent_a.flush_pm_ticks())
    assert len(agent_a.paper_positions.open_positions()) == 1
    a_position = agent_a.paper_positions.open_positions()[0]

    # Agent B — fresh process, same ledger dir.  Reads orders/fills/settlements
    # off disk on construction and rebuilds in-memory paper state.
    agent_b = Agent(dry_run=True)
    open_b = agent_b.paper_positions.open_positions()
    assert len(open_b) == 1
    b_position = open_b[0]
    assert b_position.contract_id == a_position.contract_id
    assert b_position.side == a_position.side
    assert b_position.entry_price == pytest.approx(a_position.entry_price)
    assert b_position.filled_size_usd == pytest.approx(a_position.filled_size_usd)
    # Order registry rebuilt — settlement poller can find the originating order
    assert a_position.order_ids[0] in agent_b._paper_orders_by_id


# ══════════════════════════════════════════════════════════════════════════════
# Build step 2: Polymarket un-short-circuited in PAPER mode
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_polymarket_signal_routed_to_paper_in_paper_mode(
    monkeypatch, tmp_path: Path,
):
    """Build step 2 (plan sections 3.2/4.1): the PM drop in
    OrderManager.place() is lifted in paper mode.  A passing Polymarket
    signal with captured depth now routes through the SAME FillSimulator
    Kalshi uses and produces a paper order + fill + position -- where the
    Round 8 invariant produced nothing.
    """
    agent, expiry = _seed_agent(monkeypatch, tmp_path)
    # Seed POLYMARKET feed-health so the freshness gate doesn't reject.
    agent.feed_health.record_tick(DataSource.POLYMARKET)

    poly_tick = PredictionMarketTick(
        source=DataSource.POLYMARKET,
        contract_id="poly-btc-100k",
        question="BTC above $100k?",
        strike=100_000.0,
        expiry=expiry,
        yes_bid=0.40,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
        # Captured depth so the require_nonempty_book gate passes and the
        # book-walk has levels to consume.
        order_book_yes=[(0.42, 500.0)],
        order_book_no=[(0.60, 500.0)],
        timestamp=datetime.now(timezone.utc),
    )
    agent.ingest_pm_tick(poly_tick)
    await agent.run_scan_pipeline(agent.flush_pm_ticks())

    # A paper order + full fill + open position now exist for the PM signal.
    orders = list(agent.paper_ledger.replay_orders())
    assert len(orders) == 1
    assert orders[0].platform == DataSource.POLYMARKET
    assert orders[0].side == "yes"
    fills = list(agent.paper_ledger.replay_fills())
    assert len(fills) == 1
    assert fills[0].fill_outcome == "full"
    assert fills[0].fill_price == pytest.approx(0.42)
    open_positions = agent.paper_positions.open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].platform == DataSource.POLYMARKET
    assert agent._funnel["paper_orders_placed"] == 1
    assert agent._funnel["paper_orders_filled"] == 1


@pytest.mark.asyncio
async def test_polymarket_clears_same_gates_as_kalshi(monkeypatch, tmp_path: Path):
    """PM must clear the IDENTICAL gate chain Kalshi does -- the
    un-short-circuit skips none of them.  Run the same tick shape under both
    sources and assert each yields exactly one paper order."""
    results: dict[DataSource, int] = {}
    for source in (DataSource.KALSHI, DataSource.POLYMARKET):
        sub_dir = tmp_path / source.value
        agent, expiry = _seed_agent(monkeypatch, sub_dir)
        agent.feed_health.record_tick(source)
        tick = PredictionMarketTick(
            source=source,
            contract_id=f"{source.value}-btc-100k",
            question="BTC above $100k?",
            strike=100_000.0,
            expiry=expiry,
            yes_bid=0.40,
            yes_ask=0.42,
            no_bid=0.58,
            no_ask=0.60,
            order_book_yes=[(0.42, 500.0)],
            order_book_no=[(0.60, 500.0)],
            timestamp=datetime.now(timezone.utc),
        )
        agent.ingest_pm_tick(tick)
        await agent.run_scan_pipeline(agent.flush_pm_ticks())
        results[source] = len(list(agent.paper_ledger.replay_orders()))

    # Identical gate outcome for both venues: one paper order each.
    assert results[DataSource.KALSHI] == 1
    assert results[DataSource.POLYMARKET] == 1


@pytest.mark.asyncio
async def test_polymarket_dropped_in_live_mode(monkeypatch, tmp_path: Path):
    """Live-trading guardrail unchanged: with dry_run_paper_mode=False the
    PM drop is still in force -- place() returns None and no paper state is
    created.  (Exercised directly on OrderManager since the Agent always
    runs paper mode.)"""
    from btc_pm_arb.execution.orders import OrderManager
    from btc_pm_arb.models import ArbitrageSignal, ProbabilityQuote

    expiry = datetime.now(timezone.utc) + timedelta(days=7)
    pm_quote = ProbabilityQuote(
        source=DataSource.POLYMARKET,
        contract_id="poly-btc-100k",
        strike=100_000.0,
        expiry=expiry,
        bid_prob=0.40,
        ask_prob=0.42,
        mid_prob=0.41,
        settlement_type="polymarket_spot",
        timestamp=datetime.now(timezone.utc),
    )
    opt_quote = pm_quote.model_copy(update={"source": DataSource.DERIBIT})
    signal = ArbitrageSignal(
        options_quote=opt_quote,
        pm_quote=pm_quote,
        raw_edge=0.13,
        adjusted_edge=0.13,
        trade_side="buy_yes",
        confidence=0.7,
        timestamp=datetime.now(timezone.utc),
    )

    # Live mode: dry_run_paper_mode=False -> PM dropped.
    mgr_live = OrderManager(dry_run=False, dry_run_paper_mode=False)
    assert await mgr_live.place(signal, size_usd=200.0) is None
    await mgr_live.aclose()

    # Paper mode: dry_run_paper_mode=True -> PM routed (order created).
    mgr_paper = OrderManager(dry_run=True, dry_run_paper_mode=True)
    order = await mgr_paper.place(signal, size_usd=200.0)
    assert order is not None
    assert order.platform == DataSource.POLYMARKET
    assert order.state.value == "placed"
    await mgr_paper.aclose()
