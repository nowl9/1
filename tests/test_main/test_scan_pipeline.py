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
    order_book_yes: list | None = None,
    order_book_no: list | None = None,
) -> PredictionMarketTick:
    # A realistic Kalshi tick carries some depth on each side; the depth gate
    # (require_nonempty_book) rejects signals whose crossed book is empty.
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
        order_book_yes=order_book_yes if order_book_yes is not None else [(yes_ask, 500.0)],
        order_book_no=order_book_no if order_book_no is not None else [(no_ask, 500.0)],
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
        assert payload["fired_at"] == sig.timestamp.isoformat()
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

    def test_signal_to_payload_uses_question_when_provided(self) -> None:
        """Round 9a': passing a question yields a human-readable name;
        the contract field stays the raw contract_id."""
        sig = _make_arbitrage_signal()
        payload = Agent._signal_to_payload(sig, "BTC above $100k by Jun 30?")
        assert payload["name"] == "BTC above $100k by Jun 30?"
        assert payload["contract"] == "KXBTC-26JUN30-B100000"

    def test_signal_to_payload_falls_back_to_contract_id_when_question_empty(
        self,
    ) -> None:
        """Realistic Polymarket failure mode: gamma response returns a
        blank question.  Empty string falls back to contract_id (not
        rendered as an empty title)."""
        sig = _make_arbitrage_signal()
        payload = Agent._signal_to_payload(sig, "")
        assert payload["name"] == "KXBTC-26JUN30-B100000"
        assert payload["contract"] == "KXBTC-26JUN30-B100000"

    def test_signal_to_payload_falls_back_to_contract_id_when_question_none(
        self,
    ) -> None:
        """Default-argument contract: one-positional-arg calls preserve
        the pre-9a' behavior (name == contract_id)."""
        sig = _make_arbitrage_signal()
        payload = Agent._signal_to_payload(sig)
        assert payload["name"] == "KXBTC-26JUN30-B100000"
        assert payload["contract"] == "KXBTC-26JUN30-B100000"


class TestRejectedToPayload:
    def test_rejected_to_payload_shape(self) -> None:
        edge = _make_edge_result(best_side="buy_yes", edge=0.05)
        payload = Agent._rejected_to_payload(edge, "conservative_edge 0.0500 < min 0.06")

        assert payload["id"].startswith("KXBTC-26JUN30-B100000:buy_yes:")
        assert payload["name"] == "BTC above $100,000 by Jun 30?"
        assert payload["contract"] == "KXBTC-26JUN30-B100000"
        assert payload["platform"] == "kalshi"
        assert payload["expiry"] == _utc().isoformat()
        assert payload["fired_at"] == edge.timestamp.isoformat()
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
          edge_yes_conservative = 0.55 - 0.42 = +0.13 (well above default 0.01)

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

    @pytest.mark.asyncio
    async def test_missing_originating_data_increments_counter(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Round 9a: when a passing signal's contract_id is not present in
        ``edges_by_id`` / ``tick_by_contract``, the defensive branch in
        ``run_scan_pipeline`` increments
        ``_funnel["paper_ledger_missing_originating_data"]``.

        In healthy operation this branch never fires — signals come from
        the same ``edges`` list that builds both lookups, so the keys
        always match.  The only realistic way to exercise the branch is
        to monkeypatch ``signal_filter.filter`` to return a signal whose
        ``pm_quote.contract_id`` is foreign to the edges list.

        Why this test exists: defensive code without coverage rots.  The
        Round 9a operational-telemetry contract says this counter must
        increment whenever the warning fires; a future refactor that
        accidentally desynchronises the counter from the warning would
        otherwise pass CI.
        """
        monkeypatch.setattr(
            "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
        )
        agent = Agent(dry_run=True)
        agent.feed_health.record_tick(DataSource.DERIBIT)
        agent.feed_health.record_tick(DataSource.KALSHI)

        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        agent.cache.update(
            strike=100_000.0, expiry=expiry,
            bid_prob=0.55, ask_prob=0.55, mid_prob=0.55,
            source=DataSource.DERIBIT,
        )
        # A real tick so the matcher produces a real edge — but the filter
        # output we substitute in next refers to a different contract_id,
        # so the lookups built from this edge won't contain it.
        tick = _make_pm_tick(
            expiry=expiry, yes_bid=0.40, yes_ask=0.42,
            no_bid=0.58, no_ask=0.60,
        )
        agent.ingest_pm_tick(tick)

        # Phantom signal whose contract_id is NOT among the produced edges.
        # Forces the originating_edge / originating_tick lookups to miss.
        phantom_signal = _make_arbitrage_signal(
            pm_contract_id="KXBTC-PHANTOM-NOT-IN-LOOKUP",
            source=DataSource.KALSHI,
        )
        monkeypatch.setattr(
            agent.signal_filter, "filter",
            lambda *args, **kwargs: [phantom_signal],
        )

        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        # Counter incremented exactly once for the phantom signal.
        assert agent._funnel["paper_ledger_missing_originating_data"] == 1
        # Placement still counted — the increment occurs before the
        # defensive lookup, mirroring the real code path.
        assert agent._funnel["paper_orders_placed"] == 1


# ── Rejection-path shadow fill (measurement infra) ────────────────────────────


class TestShadowFillForRejection:
    """``Agent._shadow_fill_for_rejection`` reuses the placed-order book-walk to
    give near-floor rejections a fill-adjusted edge — NOT a second, more
    optimistic fill model.  The contract stays rejected; this only records what
    its fill WOULD have been."""

    def _agent(self, monkeypatch, tmp_path) -> Agent:
        monkeypatch.setattr(
            "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
        )
        return Agent(dry_run=True)

    def test_near_floor_conservative_edge_rejection_gets_book_walked_edge(
        self, monkeypatch, tmp_path,
    ) -> None:
        agent = self._agent(monkeypatch, tmp_path)
        # Deep book at the ask → full fill at the ask price.
        tick = _make_pm_tick(yes_ask=0.543, order_book_yes=[(0.543, 500.0)])
        edge = _make_edge_result(pm_tick=tick, best_side="buy_yes", edge=0.007)

        fae, ev = agent._shadow_fill_for_rejection(edge, "conservative_edge")

        assert ev is not None
        assert ev.outcome == "full"
        assert ev.reason == "book_walk_full"
        # fair value (model-YES bid 0.55) minus the depth VWAP at the ask 0.543.
        assert fae == pytest.approx(0.55 - 0.543)

    def test_thin_book_yields_honest_partial_not_manufactured_full(
        self, monkeypatch, tmp_path,
    ) -> None:
        """The honest book-walk fills only what is available at-or-below the
        limit and STOPS at the 0.99 wall — it never crosses the limit to
        manufacture a full fill (the phantom-alpha guardrail)."""
        agent = self._agent(monkeypatch, tmp_path)
        tick = _make_pm_tick(
            yes_ask=0.543,
            order_book_yes=[(0.543, 120.0), (0.99, 1000.0)],
        )
        edge = _make_edge_result(pm_tick=tick, best_side="buy_yes", edge=0.007)

        fae, ev = agent._shadow_fill_for_rejection(edge, "conservative_edge")

        assert ev is not None
        assert ev.outcome == "partial"
        assert ev.reason == "book_walk_partial"
        # Only $120 of the $200 was lift-able at-or-below the limit; the wall
        # at 0.99 was NOT crossed.
        assert ev.fill_size_usd == pytest.approx(120.0)
        assert fae == pytest.approx(0.55 - 0.543)

    def test_empty_book_records_no_fill_reason_with_none_edge(
        self, monkeypatch, tmp_path,
    ) -> None:
        """An empty crossed book is an honest no-fill — record the simulator
        reason but NEVER invent a fill-adjusted edge."""
        agent = self._agent(monkeypatch, tmp_path)
        tick = _make_pm_tick(yes_ask=0.543, order_book_yes=[])
        edge = _make_edge_result(pm_tick=tick, best_side="buy_yes", edge=0.007)

        fae, ev = agent._shadow_fill_for_rejection(edge, "conservative_edge")

        assert fae is None
        assert ev is not None
        assert ev.reason == "empty_book"

    def test_buy_no_walks_the_no_book(self, monkeypatch, tmp_path) -> None:
        agent = self._agent(monkeypatch, tmp_path)
        # buy_no: limit = 1 - yes_bid = 0.60; walk the NO book at 0.60.
        tick = _make_pm_tick(yes_bid=0.40, order_book_no=[(0.60, 500.0)])
        edge = _make_edge_result(pm_tick=tick, best_side="buy_no", edge=0.02)

        fae, ev = agent._shadow_fill_for_rejection(edge, "conservative_edge")

        assert ev is not None
        assert ev.outcome == "full"
        # fair = 1 - model-YES ask (0.55) = 0.45; NO fill at 0.60 → negative.
        assert fae == pytest.approx(0.45 - 0.60)

    @pytest.mark.parametrize(
        "reason_key",
        ["no_positive_edge", "days_to_expiry", "empty_book", "range_product",
         "pm_spread", "match_quality"],
    )
    def test_structural_rejections_are_not_walked(
        self, monkeypatch, tmp_path, reason_key,
    ) -> None:
        agent = self._agent(monkeypatch, tmp_path)
        edge = _make_edge_result(best_side="buy_yes", edge=0.02)
        assert agent._shadow_fill_for_rejection(edge, reason_key) == (None, None)

    def test_no_best_side_is_not_walked(self, monkeypatch, tmp_path) -> None:
        agent = self._agent(monkeypatch, tmp_path)
        edge = _make_edge_result(best_side=None, edge=0.0)
        assert agent._shadow_fill_for_rejection(edge, "conservative_edge") == (
            None, None,
        )

    def test_sub_noise_edge_is_not_walked(self, monkeypatch, tmp_path) -> None:
        agent = self._agent(monkeypatch, tmp_path)
        edge = _make_edge_result(best_side="buy_yes", edge=0.004)  # < 0.005 floor
        assert agent._shadow_fill_for_rejection(edge, "conservative_edge") == (
            None, None,
        )

    @pytest.mark.asyncio
    async def test_end_to_end_near_floor_rejection_persists_fill_adjusted_edge(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Re-creates the goal's observed gap: a sub-floor contract that hits
        rejections.jsonl now carries a fill-adjusted edge via the SAME
        book-walk, not just the passers that clear the floor."""
        monkeypatch.setattr(
            "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
        )
        agent = Agent(dry_run=True)
        agent.feed_health.record_tick(DataSource.DERIBIT)
        agent.feed_health.record_tick(DataSource.KALSHI)

        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        agent.cache.update(
            strike=100_000.0, expiry=expiry,
            bid_prob=0.55, ask_prob=0.55, mid_prob=0.55,
            source=DataSource.DERIBIT,
        )
        # options mid 0.55, yes_ask 0.543 → conservative edge 0.007: above the
        # 0.005 walk floor but below the 0.01 min_conservative_edge → rejected
        # at the conservative_edge gate with a walkable book.
        tick = _make_pm_tick(
            expiry=expiry, yes_bid=0.538, yes_ask=0.543,
            order_book_yes=[(0.543, 500.0)],
        )
        agent.ingest_pm_tick(tick)

        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        rejections = list(agent.paper_ledger.replay_rejections())
        assert len(rejections) == 1
        rec = rejections[0]
        assert rec.reason_key == "conservative_edge"
        assert rec.fill_adjusted_edge == pytest.approx(0.55 - 0.543)
        assert rec.fill_outcome == "full"
        assert rec.fill_simulator_reason == "book_walk_full"
        assert agent._funnel["shadow_fill_walked"] == 1
