"""Tests for the matcher → edge → filter → confidence pipeline wiring
that runs inside ``Agent.run_scan_pipeline`` (Round 7c step 2).

Scope:
* Three unit tests for the dashboard payload helpers.
* One integration test that seeds an Agent with a minimal cache + tick
  and asserts the full chain produces ≥1 signal in ``_latest_signals``.

Per the Round 7c plan, no tests for matcher/edge/filter/confidence
themselves — they have existing coverage in tests/test_signals/.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.main import Agent
from btc_pm_arb.models import (
    ArbitrageSignal,
    DataSource,
    PredictionMarketTick,
    ProbabilityQuote,
)
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.signals.edge import EdgeResult
from btc_pm_arb.signals.matcher import MatchResult


# ── Fixture builders ──────────────────────────────────────────────────────────


def _utc(year: int = 2026, month: int = 6, day: int = 30) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _make_options_quote(
    *,
    strike: float = 100_000.0,
    expiry: datetime | None = None,
    mid: float = 0.55,
) -> ProbabilityQuote:
    return ProbabilityQuote(
        source=DataSource.DERIBIT,
        contract_id="BTC-30JUN26-100000-C",
        strike=strike,
        expiry=expiry or _utc(),
        bid_prob=mid,
        ask_prob=mid,
        mid_prob=mid,
        direction="above",
        settlement_type="deribit_twap",
        timestamp=datetime.now(timezone.utc),
    )


def _make_pm_quote(
    *,
    contract_id: str = "KXBTC-26JUN30-B100000",
    source: DataSource = DataSource.KALSHI,
    strike: float = 100_000.0,
    expiry: datetime | None = None,
    bid: float = 0.40,
    ask: float = 0.42,
) -> ProbabilityQuote:
    settlement = "kalshi_rti" if source == DataSource.KALSHI else "polymarket_spot"
    return ProbabilityQuote(
        source=source,
        contract_id=contract_id,
        strike=strike,
        expiry=expiry or _utc(),
        bid_prob=bid,
        ask_prob=ask,
        mid_prob=(bid + ask) / 2,
        direction="above",
        settlement_type=settlement,  # type: ignore[arg-type]
        timestamp=datetime.now(timezone.utc),
    )


def _make_arbitrage_signal(
    *,
    trade_side: str = "buy_yes",
    adjusted_edge: float = 0.13,
    confidence: float = 0.7,
    pm_contract_id: str = "KXBTC-26JUN30-B100000",
    source: DataSource = DataSource.KALSHI,
) -> ArbitrageSignal:
    expiry = _utc()
    return ArbitrageSignal(
        options_quote=_make_options_quote(expiry=expiry, mid=0.55),
        pm_quote=_make_pm_quote(
            contract_id=pm_contract_id, source=source, expiry=expiry,
        ),
        raw_edge=0.14,
        adjusted_edge=adjusted_edge,
        fill_adjusted_edge=None,
        trade_side=trade_side,  # type: ignore[arg-type]
        confidence=confidence,
        feed_staleness_ms={},
        vol_regime="normal",
        timestamp=datetime.now(timezone.utc),
    )


def _make_pm_tick(
    *,
    contract_id: str = "KXBTC-26JUN30-B100000",
    source: DataSource = DataSource.KALSHI,
    strike: float = 100_000.0,
    expiry: datetime | None = None,
    yes_bid: float = 0.40,
    yes_ask: float = 0.42,
    no_bid: float = 0.58,
    no_ask: float = 0.60,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=source,
        contract_id=contract_id,
        question="BTC above $100,000 by Jun 30?",
        strike=strike,
        expiry=expiry or _utc(),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        timestamp=datetime.now(timezone.utc),
    )


def _make_edge_result(
    *,
    pm_tick: PredictionMarketTick | None = None,
    best_side: str | None = "buy_yes",
    edge: float = 0.13,
) -> EdgeResult:
    """Build a synthetic EdgeResult for payload tests (no real chain run)."""
    pm_tick = pm_tick or _make_pm_tick()
    pm_quote = _make_pm_quote(
        contract_id=pm_tick.contract_id, source=pm_tick.source,
        expiry=pm_tick.expiry, bid=pm_tick.yes_bid or 0.4, ask=pm_tick.yes_ask or 0.42,
    )
    options_entry = CacheEntry(
        strike=100_000.0,
        expiry=pm_tick.expiry or _utc(),
        bid_prob=0.55,
        ask_prob=0.55,
        mid_prob=0.55,
        source=DataSource.DERIBIT,
        timestamp=datetime.now(timezone.utc),
    )
    match = MatchResult(
        pm_tick=pm_tick,
        pm_quote=pm_quote,
        options_entry=options_entry,
        matched_strike=100_000.0,
        matched_expiry=pm_tick.expiry or _utc(),
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        is_interpolated=False,
    )
    return EdgeResult(
        match=match,
        edge_yes_mid=0.14,
        edge_no_mid=-0.14,
        edge_yes_conservative=edge,
        edge_no_conservative=-edge,
        adjusted_edge_yes=edge,
        adjusted_edge_no=-edge,
        best_side=best_side,  # type: ignore[arg-type]
        best_conservative_edge=edge if best_side else 0.0,
        fill_adjusted_edge=None,
    )


# ── Unit tests: payload helpers ───────────────────────────────────────────────


class TestSignalToPayload:
    def test_signal_to_payload_shape(self) -> None:
        sig = _make_arbitrage_signal(
            trade_side="buy_yes", adjusted_edge=0.13, confidence=0.7,
        )
        payload = Agent._signal_to_payload(sig)

        # Every key the dashboard reads is present and well-typed.
        assert payload["id"].startswith("KXBTC-26JUN30-B100000:buy_yes:")
        assert payload["name"] == "KXBTC-26JUN30-B100000"
        assert payload["contract"] == "KXBTC-26JUN30-B100000"
        assert payload["platform"] == "kalshi"
        assert payload["expiry"] == _utc().isoformat()
        assert payload["side"] == "yes"
        assert payload["edge"] == pytest.approx(0.13)
        assert payload["fill_adjusted_edge"] is None
        assert payload["actionable"] is True
        assert payload["filtered"] is False
        assert payload["implied_prob"] == pytest.approx(0.55)
        assert payload["market_prob"] == pytest.approx(0.41)
        assert payload["confidence"] == pytest.approx(0.7)

    def test_signal_to_payload_side_buy_no_renders_no(self) -> None:
        sig = _make_arbitrage_signal(trade_side="buy_no")
        assert Agent._signal_to_payload(sig)["side"] == "no"


class TestRejectedToPayload:
    def test_rejected_to_payload_shape(self) -> None:
        edge = _make_edge_result(best_side="buy_yes", edge=0.05)
        payload = Agent._rejected_to_payload(edge, "conservative_edge 0.0500 < min 0.06")

        assert payload["id"].startswith("KXBTC-26JUN30-B100000:buy_yes:")
        assert payload["name"] == "BTC above $100,000 by Jun 30?"
        assert payload["contract"] == "KXBTC-26JUN30-B100000"
        assert payload["platform"] == "kalshi"
        assert payload["expiry"] == _utc().isoformat()
        assert payload["side"] == "yes"
        assert payload["edge"] == pytest.approx(0.05)
        assert payload["fill_adjusted_edge"] is None
        assert payload["actionable"] is False
        assert payload["filtered"] is True
        assert payload["rejection_reasons"] == ["conservative_edge 0.0500 < min 0.06"]
        assert payload["implied_prob"] == pytest.approx(0.55)
        assert payload["market_prob"] is not None
        assert payload["confidence"] is None

    def test_rejected_to_payload_handles_no_best_side(self) -> None:
        # When no positive edge exists on either side, best_side is None.
        edge = _make_edge_result(best_side=None, edge=0.0)
        payload = Agent._rejected_to_payload(edge, "no_positive_edge")
        # Defaults to "yes" display, id contains "none" placeholder.
        assert payload["side"] == "yes"
        assert ":none:" in payload["id"]


class TestSignalPayloadIdStability:
    def test_same_signal_same_id(self) -> None:
        sig_a = _make_arbitrage_signal()
        sig_b = _make_arbitrage_signal()
        assert Agent._signal_to_payload(sig_a)["id"] == Agent._signal_to_payload(sig_b)["id"]

    def test_different_side_different_id(self) -> None:
        sig_yes = _make_arbitrage_signal(trade_side="buy_yes")
        sig_no = _make_arbitrage_signal(trade_side="buy_no")
        id_yes = Agent._signal_to_payload(sig_yes)["id"]
        id_no = Agent._signal_to_payload(sig_no)["id"]
        assert id_yes != id_no
        assert ":buy_yes:" in id_yes
        assert ":buy_no:" in id_no


# ── Integration test: the full chain end-to-end ───────────────────────────────


class TestRunScanPipelineEndToEnd:
    @pytest.mark.asyncio
    async def test_pipeline_produces_signal_for_clear_edge(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Seed a single matching cache entry + a clearly-edged PM tick;
        assert the full chain emits exactly one passing signal payload.

        Edge math:
          options mid = 0.55, pm yes_ask = 0.42
          edge_yes_conservative = 0.55 - 0.42 = +0.13 (well above default 0.03)

        Round 8 Commit 3 made ``run_scan_pipeline`` async (it now awaits
        ``OrderManager.place``).  Test was synchronous; converted to
        async + await with ``@pytest.mark.asyncio``.  Monkeypatches the
        paper_ledger_dir to ``tmp_path`` so the agent's paper-trading
        side effects don't leak into the test runner's CWD.
        """
        monkeypatch.setattr(
            "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
        )
        agent = Agent(dry_run=True)

        # Seed feed-health timestamps so the freshness gate doesn't reject.
        # Without this, staleness_s() returns inf and every signal fails
        # _reject_feed_freshness (filters.py:196-214).
        agent.feed_health.record_tick(DataSource.DERIBIT)
        agent.feed_health.record_tick(DataSource.KALSHI)

        # Seed the probability cache with one matching grid point.
        # Use 7d-out so the expiry-bounds filter (1d ≤ T ≤ 90d) passes.
        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        agent.cache.update(
            strike=100_000.0,
            expiry=expiry,
            bid_prob=0.55,
            ask_prob=0.55,
            mid_prob=0.55,
            source=DataSource.DERIBIT,
        )

        # Inject a Kalshi tick with a clear ~13 % YES edge.
        tick = _make_pm_tick(
            expiry=expiry,
            yes_bid=0.40,
            yes_ask=0.42,
            no_bid=0.58,
            no_ask=0.60,
        )
        agent.ingest_pm_tick(tick)

        # Drain + run the pipeline (the body of what _scan_task calls).
        pm_ticks = agent.flush_pm_ticks()
        await agent.run_scan_pipeline(pm_ticks)

        # Exactly one passing signal, on YES side, with edge > 10 %.
        assert len(agent._latest_signals) == 1
        sig = agent._latest_signals[0]
        assert sig["actionable"] is True
        assert sig["filtered"] is False
        assert sig["side"] == "yes"
        assert sig["edge"] > 0.10
        assert sig["platform"] == "kalshi"
        assert sig["contract"] == "KXBTC-26JUN30-B100000"
        # Confidence was scored (not the 0.5 placeholder, but in [0, 1]).
        assert 0.0 <= sig["confidence"] <= 1.0

    @pytest.mark.asyncio
    async def test_pipeline_clears_signals_on_empty_cache(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Cold-start: empty cache → matcher returns no matches → empty signals.

        Verifies the clear-on-empty branch in run_scan_pipeline so a
        previous scan's signals don't linger when the matcher finds
        nothing on the next tick.
        """
        monkeypatch.setattr(
            "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
        )
        agent = Agent(dry_run=True)
        # Pre-stash signals as if a prior scan had populated them.
        agent._latest_signals = [{"id": "stale", "actionable": True}]

        # Inject a tick but leave the cache empty.
        tick = _make_pm_tick()
        agent.ingest_pm_tick(tick)

        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        # Stale state cleared by the matcher short-circuit branch.
        assert agent._latest_signals == []
