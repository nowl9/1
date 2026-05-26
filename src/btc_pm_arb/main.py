"""Entry point — wires all four layers into a running arbitrage agent.

Pipeline::

    DeribitFeed ──► RealizedVolTracker + VolSurface + ProbabilityCache
                                    │
    PM feeds (direct) ──────────────►ContractMatcher
    PMXT discovery (slow loop) ──────►            │
                                              EdgeCalculator
                                              (fill-adjusted)
                                                    │
                                         SignalFilter (12 criteria)
                                         ├── FeedHealthTracker
                                         ├── OddsVelocityTracker
                                         └── RealizedVolTracker
                                                    │
                                           ConfidenceScorer
                                                    │
                                          RiskManager (pre-trade)
                                                    │
                                          OrderManager ──► positions / settlement
                                                    │
                                           Dashboard (FastAPI + WS)

Modes
-----
* DRY RUN (default): signals and theoretical orders logged; nothing submitted.
* LIVE: set ``dry_run=False``; requires valid API keys.

Start::

    python -m btc_pm_arb.main          # dry-run mode
    DASHBOARD_TOKEN=secret uvicorn btc_pm_arb.server.app:app --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import structlog
import uvicorn

from btc_pm_arb.config import settings
from btc_pm_arb.execution.fill_simulator import BookSnapshot, FillSimulator
from btc_pm_arb.execution.orders import OrderManager, Order
from btc_pm_arb.execution.paper_ledger import (
    BookLevel,
    PaperLedger,
    PaperOrderRecord,
    PaperRejectionRecord,
)
from btc_pm_arb.execution.paper_positions import PaperPosition, PaperPositionTracker
from btc_pm_arb.execution.paper_settlement import KalshiSettlementPoller
from btc_pm_arb.execution.positions import PositionTracker
from btc_pm_arb.execution.risk import RiskConfig, RiskManager
from btc_pm_arb.execution.settlement import SettlementMonitor
from btc_pm_arb.feeds.deribit import DeribitFeed
from btc_pm_arb.feeds.discovery import MarketDiscovery, run_discovery_loop
from btc_pm_arb.feeds.health import FeedHealthTracker
from btc_pm_arb.feeds.kalshi import KalshiFeed
from btc_pm_arb.feeds.polymarket import PolymarketFeed
from btc_pm_arb.feeds.recorder import FrameRecorder
from btc_pm_arb.models import ArbitrageSignal, DataSource, OptionTick, PredictionMarketTick
from btc_pm_arb.pricing.cache import ProbabilityCache
from btc_pm_arb.pricing.digital_pricer import DigitalPricer
from btc_pm_arb.pricing.realized_vol import RealizedVolTracker
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.server.app import create_app
from btc_pm_arb.server.state import SharedState
from btc_pm_arb.signals.confidence import ConfidenceScorer
from btc_pm_arb.signals.edge import EdgeCalculator, EdgeResult
from btc_pm_arb.signals.filters import FilterConfig, SignalFilter, _extract_reason_key
from btc_pm_arb.signals.matcher import ContractMatcher
from btc_pm_arb.signals.velocity import OddsVelocityTracker

log: structlog.BoundLogger = structlog.get_logger("main")

_SCAN_INTERVAL_SECS: float = 5.0
_ORDER_REFRESH_SECS: float = 30.0
_BASE_SIZE_USD: float = 200.0
_DASHBOARD_PORT: int = 8000


# ── Logging ───────────────────────────────────────────────────────────────────

def _configure_logging() -> None:
    log_level = getattr(logging, settings.log_level, logging.INFO)
    if settings.log_format == "json":
        processors: list = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ]
    structlog.configure(
        processors=processors,  # type: ignore[arg-type]
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ── Agent ─────────────────────────────────────────────────────────────────────

class Agent:
    """Container for all stateful pipeline components."""

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self.surface = VolSurface()
        self.cache = ProbabilityCache()
        self.pricer = DigitalPricer()
        self.rv_tracker = RealizedVolTracker()
        self.feed_health = FeedHealthTracker()
        self.odds_tracker = OddsVelocityTracker()
        self.matcher = ContractMatcher()
        self.edge_calc = EdgeCalculator()
        self.signal_filter = SignalFilter(FilterConfig(
            min_conservative_edge=settings.min_edge,
            min_days_to_expiry=float(settings.min_days_to_expiry),
            max_days_to_expiry=float(settings.max_days_to_expiry),
        ))
        self.confidence_scorer = ConfidenceScorer()
        self.tracker = PositionTracker()
        self.risk = RiskManager(RiskConfig(
            max_position_per_contract_usd=settings.max_position_usd,
            max_total_exposure_usd=settings.max_total_exposure_usd,
        ))
        # Round 8 Commit 2: dry_run_paper_mode=True suppresses
        # KalshiExecutor's optimistic auto-fill on refresh() so the paper
        # FillSimulator owns the FILLED transition instead.  See
        # execution/orders.py KalshiExecutor docstring.
        self.order_mgr = OrderManager(dry_run=dry_run, dry_run_paper_mode=dry_run)
        self.settlement_monitor = SettlementMonitor(self.tracker)

        live_token = secrets.token_urlsafe(16)
        self.shared_state = SharedState(dry_run=dry_run, live_mode_token=live_token)

        self._pending_ticks: list[OptionTick] = []
        # Buffer of prediction-market ticks awaiting matcher consumption.
        # Populated by Kalshi (Round 6) and Polymarket (Round 7b) feed tasks
        # via ``ingest_pm_tick``.  Drained per scan tick by
        # ``run_scan_pipeline`` and ``mark_to_market``; bounded deque
        # prevents unbounded growth across feed disconnections.
        self._pending_pm_ticks: deque[PredictionMarketTick] = deque(maxlen=10_000)
        # First-observed PM tick per source — gates the diagnostic log in
        # ``ingest_pm_tick`` so we get exactly ONE shape sample per source
        # per process.  Used to verify upstream tick quality (Kalshi cents
        # field migration, Polymarket _build_tick output) without flooding
        # logs.
        self._first_tick_logged: set[DataSource] = set()
        # Latest dashboard payload from ``run_scan_pipeline``.  Updated
        # once per scan tick (5 s cadence); read by ``_push_state_update``
        # into ``shared_state.signals`` for the WebSocket snapshot.  Empty
        # list during cold-start (no fitted smiles → empty cache → no
        # matches).  Defensive copy on read keeps shared-state independent.
        self._latest_signals: list[dict] = []

        # ── Round 8 Commit 3: Paper-trading components ────────────────────────
        self.paper_ledger = PaperLedger(settings.paper_ledger_dir)
        self.fill_simulator = FillSimulator()
        self.paper_positions = PaperPositionTracker()
        # Order registry keyed by client_order_id — populated as the agent
        # places paper orders, and re-populated from disk on startup so the
        # settlement poller can still look up theoretical_edge for positions
        # opened in a previous process lifetime.
        self._paper_orders_by_id: dict[str, PaperOrderRecord] = {}
        # Signal-funnel counters surfaced via SharedState.signal_fired_skipped.
        # All increments happen from the scan task (via run_scan_pipeline);
        # serialized by the single event loop, no locking required.
        self._funnel: dict[str, int] = {
            "signals_observed_total": 0,
            "signals_passed_filter": 0,
            "signals_rejected_filter": 0,
            "paper_orders_placed": 0,
            "paper_orders_filled": 0,
            "paper_orders_no_fill": 0,
            # Round 9 Commit 9a: counter for the defensive
            # paper_ledger.missing_originating_data warning at the place()
            # → record() handoff in run_scan_pipeline.  Should always
            # remain 0 in healthy operation; non-zero indicates a
            # passing-signal contract_id that wasn't in the
            # edges_by_id / tick_by_contract lookups (a bug worth seeing).
            "paper_ledger_missing_originating_data": 0,
        }
        # Replay paper-trading state from disk so positions and the orders
        # registry persist across restarts (the load-bearing
        # replay/idempotency invariant from Commit 1's tests).  Done inline
        # rather than via PaperPositionTracker.replay_from_disk so we can
        # populate _paper_orders_by_id from the same file scan — avoids
        # double-reading orders.jsonl and double-incrementing the ledger's
        # n_records_loaded counter.
        for _order_rec in self.paper_ledger.replay_orders():
            self._paper_orders_by_id[_order_rec.client_order_id] = _order_rec
        for _fill_rec in self.paper_ledger.replay_fills():
            _matched = self._paper_orders_by_id.get(_fill_rec.client_order_id)
            if _matched is not None:
                self.paper_positions.record_fill(
                    order_record=_matched, fill_record=_fill_rec,
                )
        for _settle_rec in self.paper_ledger.replay_settlements():
            self.paper_positions.settle(_settle_rec)

        # Kalshi paper-settlement poller — runs every 60 s in its own
        # task.  Detects contract resolutions and calls back into
        # paper_positions.settle() + paper_ledger.append_settlement().
        # Looks up theoretical_edge via the orders registry above.
        self.paper_settlement_poller = KalshiSettlementPoller(
            tracker=self.paper_positions,
            ledger=self.paper_ledger,
            get_order_record=lambda cid: self._paper_orders_by_id.get(cid),
            base_url=settings.kalshi_base_url,
            key_path=settings.kalshi_private_key_path,
            key_id=settings.kalshi_api_key_id,
        )

    def ingest_tick(self, tick: OptionTick) -> None:
        self._pending_ticks.append(tick)
        self.feed_health.record_tick(DataSource.DERIBIT)
        # Throttle the RV tracker to at most 1 Hz: every option tick carries
        # the same BTC index price (the index updates only a few times per
        # second), but we receive hundreds of option-tick events per second
        # across 912 instruments — calling rv_tracker.update on every one is
        # both redundant and, because update() is O(N) over its data deque,
        # the proximate cause of the event-loop starvation that produced
        # 30 s heartbeat-RPC timeouts and 252 s connection deaths.  See
        # diagnostic round 5 trace: rv() takes ~0.1 ms at N=1000 but ~7 ms
        # at N=50 000, called per option tick, saturated the consumer task
        # within ~90 s of operation.
        self.rv_tracker.maybe_update(tick.index_price)

    def flush_ticks(self) -> set:
        if not self._pending_ticks:
            return set()
        dirty = self.surface.update(self._pending_ticks)
        self._pending_ticks.clear()
        return dirty

    def ingest_pm_tick(self, tick: PredictionMarketTick) -> None:
        """Stage a prediction-market tick for matcher consumption.

        Called from ``_kalshi_task`` (Round 6) and ``_polymarket_task``
        (Round 7b).  Buffers the tick; the matcher → edge → filter →
        confidence → place + simulate + record paper-trading pipeline
        drains the buffer once per scan tick (5 s cadence) inside
        :meth:`run_scan_pipeline`.  As of Round 8 Commit 3 the dry-run
        gate is lifted: passing signals flow through ``OrderManager.place``
        and the FillSimulator records intent/fill/position into the
        paper-trading ledger.

        The first tick observed per source is logged at INFO under
        ``pm_tick.first_observed`` with the full pydantic dump — a
        permanent diagnostic for upstream tick-shape regressions
        (Kalshi cents-field migration, Polymarket _build_tick output).
        Gated by ``_first_tick_logged`` so it fires exactly once per
        source per process.
        """
        if tick.source not in self._first_tick_logged:
            self._first_tick_logged.add(tick.source)
            log.info(
                "pm_tick.first_observed",
                source=tick.source.value,
                tick=tick.model_dump(mode="json"),
            )
        self._pending_pm_ticks.append(tick)

    def flush_pm_ticks(self) -> list[PredictionMarketTick]:
        """Drain pending PM ticks for batch matcher processing (future)."""
        if not self._pending_pm_ticks:
            return []
        out = list(self._pending_pm_ticks)
        self._pending_pm_ticks.clear()
        return out

    def update_cache_from_surface(self, dirty_expiries: set) -> None:
        for expiry in dirty_expiries:
            smile = self.surface.get_smile(expiry)
            if smile is None or smile.forward is None:
                continue
            strikes = {
                t.strike
                for t in self.surface._ticks.values()
                if t.expiry == expiry and t.mark_iv is not None
            }
            for strike in strikes:
                price = self.pricer.price_from_surface(strike, expiry, self.surface)
                if price is None:
                    continue
                self.cache.update(
                    strike=strike,
                    expiry=expiry,
                    bid_prob=price.bid,
                    ask_prob=price.ask,
                    mid_prob=price.mid,
                    source=DataSource.DERIBIT,
                )

    # ── Round 7c step 2 / Round 8 Commit 3: matcher → edge → filter → ────────
    # confidence → place + simulate + record paper-trading pipeline.

    async def run_scan_pipeline(self, pm_ticks: list[PredictionMarketTick]) -> None:
        """Drain → match → edge → filter → confidence → emit + paper-trade.

        Called from ``_scan_task`` once per scan cadence with the buffered
        PM ticks.  Populates ``self._latest_signals`` with a unified list
        of passing + rejected signal payloads for the dashboard.

        Round 8 Commit 3: lifted the dry-observation gate.  Each passing
        signal flows through ``OrderManager.place()`` (still
        Polymarket-short-circuited and dedup-gated as before).  When dry_run
        is True, the resulting order is recorded in the paper-trading ledger
        and the FillSimulator evaluates an at-or-better-than-best fill
        against the originating tick's snapshot.  When dry_run is False
        (live mode — out of scope this round), the place call still happens
        but the paper-trading recording branch is skipped.

        Async because :meth:`OrderManager.place` is async.
        """
        # Feed the velocity tracker.  ``yes_mid`` is a @property that
        # returns None unless both yes_bid and yes_ask are non-None;
        # the guard below is the safe form (no double-counting on
        # one-sided books).
        for t in pm_ticks:
            if t.yes_mid is not None:
                self.odds_tracker.update(t.contract_id, t.yes_mid)

        matches = self.matcher.batch_match(pm_ticks, self.cache)
        if not matches:
            # Cold-start (empty cache → no matches) or no overlap with
            # tracked PM contracts.  Clear so the dashboard reflects
            # "nothing actionable" rather than stale state.
            self._latest_signals = []
            return

        edges = [self.edge_calc.compute(m, surface=self.surface) for m in matches]

        # Funnel: every match-resulting edge counts as one observed signal.
        self._funnel["signals_observed_total"] += len(edges)

        # Originating-tick lookup for paper-order snapshot capture.  Built
        # from each edge's stored ``match.pm_tick`` rather than from
        # ``pm_ticks`` directly so the snapshot is guaranteed to be the
        # exact tick the matcher consumed (defends against duplicate-tick
        # reordering in the input list).
        tick_by_contract: dict[str, PredictionMarketTick] = {
            e.match.pm_tick.contract_id: e.match.pm_tick for e in edges
        }

        # ``positions`` is the input to the correlated-exposure filter.
        # Round 8 Commit 3 keeps it empty (paper positions are tracked
        # separately in self.paper_positions and don't feed this gate);
        # the criterion no-ops on empty dict.
        positions: dict[str, float] = {}

        passing = self.signal_filter.filter(
            edges,
            surface=self.surface,
            positions=positions,
            feed_health=self.feed_health,
            odds_tracker=self.odds_tracker,
            rv_tracker=self.rv_tracker,
        )

        self._funnel["signals_passed_filter"] += len(passing)

        # Backfill confidence onto each passing signal from its
        # originating EdgeResult.  ArbitrageSignal is mutable (no
        # ``frozen=True`` on the model); in-place attribute assignment
        # is safe.
        edges_by_id = {e.match.pm_tick.contract_id: e for e in edges}
        for sig in passing:
            e = edges_by_id.get(sig.pm_quote.contract_id)
            if e is not None:
                sig.confidence = self.confidence_scorer.score(
                    e, surface=self.surface
                )

        # Collect rejection reasons for non-passing edges.  Verified
        # read-only on trackers: ``signals/filters.py`` contains zero
        # ``.update(`` calls; criterion functions only call read-only
        # query methods (velocity_at, effective_min_edge, current_regime,
        # staleness_s).  Safe to invoke per non-passing edge — no
        # double-counting risk.
        passing_ids = {s.pm_quote.contract_id for s in passing}
        rejected: list[tuple[EdgeResult, str]] = []
        for e in edges:
            if e.match.pm_tick.contract_id in passing_ids:
                continue
            reason = self.signal_filter.explains(
                e,
                surface=self.surface,
                positions=positions,
                feed_health=self.feed_health,
                odds_tracker=self.odds_tracker,
                rv_tracker=self.rv_tracker,
            )
            if reason is not None:
                rejected.append((e, reason))

        self._funnel["signals_rejected_filter"] += len(rejected)

        # Round 9c: persist per-event rejections for tail_funnel's rolling
        # 1h/6h reject-rate surface and 9d2's univariate-cuts pass.  Bucket
        # key matches SignalFilter.rejection_counts' keys; full reason is
        # kept verbatim for forensics.  vol_regime is taken from the
        # tracker at write time — consistent with _to_arbitrage_signal's
        # source-side for the passing-signal path.
        for _edge, _reason in rejected:
            self.paper_ledger.append_rejection(
                PaperRejectionRecord(
                    timestamp=_edge.timestamp,
                    contract_id=_edge.match.pm_tick.contract_id,
                    platform=_edge.match.pm_tick.source,
                    reason_key=_extract_reason_key(_reason),
                    full_reason=_reason,
                    best_conservative_edge=_edge.best_conservative_edge,
                    vol_regime=self.rv_tracker.current_regime().value,
                )
            )

        self._latest_signals = self._build_signal_payloads(
            passing, rejected, edges_by_id,
        )

        for sig in passing:
            log.info(
                "signal.observed",
                contract=sig.pm_quote.contract_id,
                platform=sig.pm_quote.source.value,
                side=sig.trade_side,
                adjusted_edge=round(sig.adjusted_edge, 4),
                fill_adjusted_edge=(
                    round(sig.fill_adjusted_edge, 4)
                    if sig.fill_adjusted_edge is not None else None
                ),
                confidence=round(sig.confidence, 3),
                pm_mid=round(sig.pm_quote.mid_prob, 4),
                options_mid=round(sig.options_quote.mid_prob, 4),
            )

            # ── Round 8 Commit 3: dry-run-gated paper-order pipeline ─────────
            # In dry_run mode: place the order through OrderManager (which
            # still short-circuits Polymarket and dedupes by signal
            # fingerprint), then record + simulate + persist.  In live mode
            # this branch is skipped — live execution wiring is a future
            # round.
            if self.dry_run:
                order = await self.order_mgr.place(
                    sig, size_usd=_BASE_SIZE_USD,
                )
                if order is not None:
                    self._funnel["paper_orders_placed"] += 1
                    originating_edge = edges_by_id.get(sig.pm_quote.contract_id)
                    originating_tick = tick_by_contract.get(sig.pm_quote.contract_id)
                    if originating_edge is None or originating_tick is None:
                        # Defensive: should never happen — the signal came
                        # from a passing edge whose tick is in the lookup.
                        # Round 9 Commit 9a: increment counter so the
                        # condition is visible on the dashboard rather than
                        # only in the log stream — silent fires were the
                        # explicit Round 8 Commit 2 deferral.
                        self._funnel["paper_ledger_missing_originating_data"] += 1
                        log.warning(
                            "paper_ledger.missing_originating_data",
                            contract_id=sig.pm_quote.contract_id,
                            edge_present=originating_edge is not None,
                            tick_present=originating_tick is not None,
                        )
                    else:
                        self._record_paper_order(
                            order=order,
                            signal=sig,
                            edge_result=originating_edge,
                            tick=originating_tick,
                        )

    def _record_paper_order(
        self,
        *,
        order: Order,
        signal: ArbitrageSignal,
        edge_result: EdgeResult,
        tick: PredictionMarketTick,
    ) -> None:
        """Build the paper order record, simulate fill, and persist both.

        Called from ``run_scan_pipeline``'s dry-run gate.  Single source
        of truth for the order intent → fill simulation → ledger
        round-trip; keeps the scan pipeline body readable.
        """
        order_record = self._build_paper_order_record(
            order=order, signal=signal, edge_result=edge_result, tick=tick,
        )
        self.paper_ledger.append_order(order_record)
        self._paper_orders_by_id[order.client_order_id] = order_record

        snapshot = BookSnapshot.from_order_record(order_record)
        evaluation = self.fill_simulator.evaluate(
            side=order.side,  # type: ignore[arg-type]
            limit_price=order.limit_price,
            size_usd=order.size_usd,
            snapshot=snapshot,
        )
        fill_record = self.fill_simulator.build_fill_record(
            client_order_id=order.client_order_id,
            evaluation=evaluation,
            filled_at=datetime.now(timezone.utc),
        )
        self.paper_ledger.append_fill(fill_record)
        self.paper_positions.record_fill(
            order_record=order_record, fill_record=fill_record,
        )

        if evaluation.outcome == "full":
            self._funnel["paper_orders_filled"] += 1
        elif evaluation.outcome == "no_fill":
            self._funnel["paper_orders_no_fill"] += 1

    def _build_paper_order_record(
        self,
        *,
        order: Order,
        signal: ArbitrageSignal,
        edge_result: EdgeResult,
        tick: PredictionMarketTick,
    ) -> PaperOrderRecord:
        """Synthesise a :class:`PaperOrderRecord` from runtime objects.

        Match-quality fields come from the originating ``EdgeResult.match``
        so Round 9 calibration can study the relationship between match
        gap and realised P&L.  Order-book depth is converted from the
        tick's ``list[tuple[float, float]]`` shape into named-field
        :class:`BookLevel` instances per the Commit-1 forward-compat
        decision (see paper_ledger.py BookLevel docstring).
        """
        m = edge_result.match
        return PaperOrderRecord(
            client_order_id=order.client_order_id,
            signal_fingerprint=self.order_mgr._signal_fingerprint(signal),
            created_at=order.created_at,
            platform=order.platform,
            contract_id=order.contract_id,
            side=order.side,  # type: ignore[arg-type]
            size_usd=order.size_usd,
            limit_price=order.limit_price,
            raw_edge=signal.raw_edge,
            adjusted_edge=signal.adjusted_edge,
            fill_adjusted_edge=signal.fill_adjusted_edge,
            confidence=signal.confidence,
            vol_regime=signal.vol_regime,
            feed_staleness_ms=dict(signal.feed_staleness_ms),
            strike_gap_pct=m.strike_gap_pct,
            expiry_gap_hours=m.expiry_gap_hours,
            match_quality=m.match_quality,
            pm_yes_bid=tick.yes_bid,
            pm_yes_ask=tick.yes_ask,
            pm_no_bid=tick.no_bid,
            pm_no_ask=tick.no_ask,
            order_book_yes=[
                BookLevel(price=p, size_usd=s) for p, s in tick.order_book_yes
            ],
            order_book_no=[
                BookLevel(price=p, size_usd=s) for p, s in tick.order_book_no
            ],
            expiry=signal.pm_quote.expiry,
            dry_run=self.dry_run,
        )

    def _build_signal_payloads(
        self,
        passing: list[ArbitrageSignal],
        rejected: list[tuple[EdgeResult, str]],
        edges_by_id: dict[str, EdgeResult],
    ) -> list[dict]:
        """Build a unified, capped, sorted payload list for the dashboard.

        Passing signals first (capped at 50), then rejected (capped at
        50), each sorted by ``abs(edge)`` descending so the most
        material entries appear first in their group.

        ``edges_by_id`` lets the passing-side payload recover the
        human-readable ``pm_tick.question`` (lost when ``ArbitrageSignal``
        only carries the ``ProbabilityQuote``).  A miss falls back to
        the contract_id — mirrors ``_rejected_to_payload``'s
        ``pm.question or pm.contract_id`` shape so Polymarket signals
        no longer surface raw token IDs as their dashboard title.
        """
        def _passing_payloads() -> list[dict]:
            out: list[dict] = []
            for s in passing:
                edge = edges_by_id.get(s.pm_quote.contract_id)
                question = edge.match.pm_tick.question if edge is not None else None
                out.append(self._signal_to_payload(s, question))
            return out

        pass_payload = sorted(
            _passing_payloads(),
            key=lambda d: abs(d.get("edge") or 0.0),
            reverse=True,
        )[:50]
        rej_payload = sorted(
            (self._rejected_to_payload(e, r) for (e, r) in rejected),
            key=lambda d: abs(d.get("edge") or 0.0),
            reverse=True,
        )[:50]
        return pass_payload + rej_payload

    @staticmethod
    def _signal_to_payload(
        sig: ArbitrageSignal, question: str | None = None,
    ) -> dict:
        """Render a passing ArbitrageSignal as the dashboard schema dict.

        Schema mirrors what ``server/static/index.html`` reads from
        ``snap.signals[]`` (TOP SIGNALS panel + ALL SIGNALS tab).

        ``question`` is the human-readable contract title from the
        originating ``PredictionMarketTick``; falls back to
        ``contract_id`` when None or empty.  Mirrors the
        ``pm.question or pm.contract_id`` shape ``_rejected_to_payload``
        uses, so Polymarket signals no longer surface raw token IDs
        as their dashboard title.

        Expiry None-guard mirrors ``_rejected_to_payload`` for
        consistency.  ``ProbabilityQuote.expiry`` is non-Optional in the
        model, so the guard is purely defensive.
        """
        expiry_iso = (
            sig.pm_quote.expiry.isoformat() if sig.pm_quote.expiry else None
        )
        return {
            "id": f"{sig.pm_quote.contract_id}:{sig.trade_side}:{expiry_iso or ''}",
            "name": question or sig.pm_quote.contract_id,
            "contract": sig.pm_quote.contract_id,
            "platform": sig.pm_quote.source.value,
            "expiry": expiry_iso,
            "fired_at": sig.timestamp.isoformat(),
            "side": "yes" if sig.trade_side == "buy_yes" else "no",
            "edge": sig.adjusted_edge,
            "fill_adjusted_edge": sig.fill_adjusted_edge,
            "actionable": True,
            "filtered": False,
            "implied_prob": sig.options_quote.mid_prob,
            "market_prob": sig.pm_quote.mid_prob,
            "confidence": sig.confidence,
        }

    @staticmethod
    def _rejected_to_payload(edge: EdgeResult, reason: str) -> dict:
        """Render a filter-rejected EdgeResult as the dashboard schema dict.

        Used for the ALL SIGNALS tab to surface why an edge didn't pass.
        ``confidence`` is None (not scored — saves CPU on rejected items).
        """
        pm = edge.match.pm_tick
        # ``best_side`` is Literal["buy_yes", "buy_no"] | None; default
        # to "yes" display when None (no positive edge on either side).
        side = "yes" if (edge.best_side or "buy_yes") == "buy_yes" else "no"
        expiry_iso = pm.expiry.isoformat() if pm.expiry else None
        return {
            "id": f"{pm.contract_id}:{edge.best_side or 'none'}:{expiry_iso or ''}",
            "name": pm.question or pm.contract_id,
            "contract": pm.contract_id,
            "platform": pm.source.value,
            "expiry": expiry_iso,
            "fired_at": edge.timestamp.isoformat(),
            "side": side,
            "edge": edge.best_conservative_edge,
            "fill_adjusted_edge": edge.fill_adjusted_edge,
            "actionable": False,
            "filtered": True,
            "rejection_reasons": [reason],
            "implied_prob": edge.match.options_entry.mid_prob,
            "market_prob": edge.match.pm_quote.mid_prob,
            "confidence": None,
        }

    async def _push_state_update(self) -> None:
        """Push current layer state into SharedState for the dashboard."""
        import math
        import time

        now = time.time()

        # ── Feeds ─────────────────────────────────────────────────────────────
        staleness = self.feed_health.all_staleness_ms()
        feeds: dict = {}
        for src_str, ms in staleness.items():
            if ms == float("inf"):
                feed_status = "disconnected"
                last_tick = 0.0
                latency_ms = 0
            elif ms > 5_000:
                feed_status = "stale"
                last_tick = now - ms / 1000.0
                latency_ms = int(ms)
            else:
                feed_status = "ok"
                last_tick = now - ms / 1000.0
                latency_ms = int(ms)
            feeds[src_str] = {
                "status": feed_status,
                "latency_ms": latency_ms,
                "last_tick": last_tick,
                "is_stale": ms > 5_000 or ms == float("inf"),
            }

        # ── Vol surface summary ────────────────────────────────────────────────
        by_expiry: dict = {}
        last_fit_ts: float | None = None
        for expiry in self.surface.all_expiries():
            smile = self.surface.get_smile(expiry)
            if smile:
                by_expiry[expiry.isoformat()] = {
                    "rmse": round(smile.fit_rmse, 4) if smile.fit_rmse != float("inf") else None,
                    "rho": round(smile.params.rho, 3) if smile.params else None,
                    "n_options": smile.n_options,
                    "forward": smile.forward,
                }
                if last_fit_ts is None:
                    last_fit_ts = now

        slices = list(by_expiry.values())
        rmses = [v["rmse"] for v in slices if v.get("rmse") is not None]
        rhos = [v["rho"] for v in slices if v.get("rho") is not None]
        vol_surface = {
            "svi_rmse": round(sum(rmses) / len(rmses), 4) if rmses else None,
            "active_smiles": len(slices),
            "rho": round(sum(rhos) / len(rhos), 3) if rhos else None,
            "last_fit_timestamp": last_fit_ts,
            "by_expiry": by_expiry,
        }

        # ── Volatility regime ─────────────────────────────────────────────────
        rv_1h = self.rv_tracker.rv(1.0)
        rv_24h = self.rv_tracker.rv(24.0)
        volatility_regime = {
            "current": str(self.rv_tracker.current_regime()),
            "rv_1h": round(rv_1h, 4) if rv_1h is not None else None,
            "rv_24h": round(rv_24h, 4) if rv_24h is not None else None,
            "effective_min_edge": round(
                self.rv_tracker.effective_min_edge(settings.min_edge), 4
            ),
        }

        # ── Positions / performance ────────────────────────────────────────────
        positions = self.tracker.all_snapshots()
        perf = self.tracker.performance_summary()
        settlements = [p.snapshot() for p in self.tracker.closed_positions()]

        # ── Round 8 Commit 3: Paper-trading dashboard payload ──────────────────
        paper_open = [
            self._paper_position_payload(p)
            for p in self.paper_positions.open_positions()
        ]
        paper_settled = [
            self._paper_settlement_payload(p)
            for p in self.paper_positions.closed_positions()
        ]
        # Cap settled list at 50 most-recent for the dashboard.  Sort by
        # updated_at so the most recently settled appear first.
        paper_settled.sort(
            key=lambda d: d.get("settled_at") or "",
            reverse=True,
        )
        paper_settled = paper_settled[:50]
        paper_perf = self.paper_positions.performance_summary()
        funnel: dict[str, Any] = dict(self._funnel)
        # Round 9 Commit 9a: surface per-reason filter rejection counts on
        # the dashboard as flat ``reject_<key>`` entries (NOT a nested
        # dict).  Operational telemetry — without it, the next time the
        # ledger is unexpectedly empty we're back to grepping DEBUG logs.
        # Flat shape chosen because the dashboard rendering code was
        # written against a flat funnel dict; nesting would require
        # frontend changes to display.  The ``reject_`` prefix is
        # namespace-safe against the existing keys in ``self._funnel``
        # (none of which start with ``reject_``).  Bucket keys are the
        # stable single-token names from signals/filters.py:_extract_reason_key
        # (e.g. ``conservative_edge``, ``pm_spread``, ``deribit_feed_stale``).
        for reason_key, count in self.signal_filter.rejection_counts.items():
            funnel[f"reject_{reason_key}"] = count

        async with self.shared_state.write() as s:
            s.feeds = feeds
            s.vol_surface = vol_surface
            s.volatility_regime = volatility_regime
            # Defensive copy: keeps the snapshot independent of any
            # subsequent run_scan_pipeline rewrite of _latest_signals.
            s.signals = list(self._latest_signals)
            s.positions = positions
            s.positions_summary = perf
            s.settlements = settlements
            s.performance = perf

            # Round 8 Commit 3: paper-trading panels.
            s.paper_open_positions = paper_open
            s.paper_settlements = paper_settled
            s.paper_performance = paper_perf
            s.signal_fired_skipped = funnel

            # Risk config snapshot
            cfg = self.risk.config
            s.risk_config = {
                "max_position_per_contract_usd": cfg.max_position_per_contract_usd,
                "max_total_exposure_usd": cfg.max_total_exposure_usd,
                "max_open_positions": cfg.max_open_positions,
                "min_confidence": cfg.min_confidence,
            }

            # Latest BTC price
            if self.rv_tracker.newest_ts is not None and self.rv_tracker.n_points > 0:
                s.btc_price = math.exp(self.rv_tracker._data[-1][1])

    def _paper_position_payload(self, pos: PaperPosition) -> dict:
        """Render a paper position in the dashboard's PositionRow schema.

        Adds the alias keys that ``server/static/index.html``'s
        ``PositionRow`` reads (``name``, ``contract``, ``mark_price``,
        ``size``, ``notional``, ``pnl``) on top of the canonical
        :meth:`PaperPosition.snapshot` fields.
        """
        snap = pos.snapshot()
        return {
            **snap,
            "id": f"{pos.contract_id}:{pos.side}",
            "name": pos.contract_id,
            "contract": pos.contract_id,
            "mark_price": snap["current_mid"],
            "size": snap["filled_size_usd"],
            "notional": snap["filled_size_usd"],
            "pnl": snap["unrealized_pnl"],
        }

    def _paper_settlement_payload(self, pos: PaperPosition) -> dict:
        """Render a closed paper position in the dashboard's settle-row schema.

        Derives ``outcome``, ``edge_captured``, and ``theoretical_edge``
        from the position's stored realized_pnl + the originating order
        record's adjusted_edge.  The dashboard's existing settlement
        renderer reads ``result``/``outcome``, ``pnl``/``realized_pnl``,
        ``edge_captured``, ``edge_theoretical``/``theoretical_edge``.
        """
        order_record = None
        if pos.order_ids:
            order_record = self._paper_orders_by_id.get(pos.order_ids[0])
        theoretical_edge = order_record.adjusted_edge if order_record else 0.0

        if pos.realized_pnl > 1e-4:
            outcome = "win"
        elif pos.realized_pnl < -1e-4:
            outcome = "loss"
        else:
            outcome = "push"

        # edge_captured = realised payout-vs-entry / theoretical edge.
        # Guard against zero edge (avoid div-by-zero); 0.0 is the
        # well-defined sentinel the existing renderer treats as "—".
        if pos.settlement_price is not None:
            payout = (
                pos.settlement_price if pos.side == "yes"
                else 1.0 - pos.settlement_price
            )
            actual_edge = payout - pos.entry_price
            edge_captured = (
                actual_edge / theoretical_edge
                if abs(theoretical_edge) > 1e-9 else 0.0
            )
        else:
            edge_captured = 0.0

        return {
            "id": f"{pos.contract_id}:{pos.side}:settled",
            "name": pos.contract_id,
            "contract": pos.contract_id,
            "platform": pos.platform.value,
            "side": pos.side,
            "outcome": outcome,
            "result": outcome,
            "realized_pnl": pos.realized_pnl,
            "pnl": pos.realized_pnl,
            "theoretical_edge": theoretical_edge,
            "edge_theoretical": theoretical_edge,
            "edge_captured": edge_captured,
            "settlement_price": pos.settlement_price,
            "settled_at": (
                pos.updated_at.isoformat() if pos.updated_at else None
            ),
        }


# ── Tasks ──────────────────────────────────────────────────────────────────────

async def _deribit_task(
    agent: Agent,
    stop_event: asyncio.Event,
    *,
    recorder: FrameRecorder | None = None,
) -> None:
    log.info("deribit_task.starting", url=settings.deribit_url)
    while not stop_event.is_set():
        try:
            async with DeribitFeed(
                url=settings.deribit_url, recorder=recorder,
            ) as feed:
                async for tick in feed.ticks():
                    if stop_event.is_set():
                        return
                    agent.ingest_tick(tick)
        except Exception as exc:
            if stop_event.is_set():
                return
            log.warning("deribit_task.reconnecting", error=str(exc))
            await asyncio.sleep(2.0)
    log.info("deribit_task.stopped")


async def _kalshi_task(
    agent: Agent,
    stop_event: asyncio.Event,
    *,
    recorder: FrameRecorder | None = None,
) -> None:
    """Kalshi REST market-data feed task — Round 6 / Issue 4.

    Routes every successful HTTP response into ``feed_health.record_tick``
    via the feed's ``on_alive`` callback.  This is what makes the dashboard
    show Kalshi as OK even when the demo BTC market universe is empty
    (zero markets returned → zero ticks yielded; without the callback the
    feed would look DISCONNECTED forever despite working correctly).
    """
    log.info("kalshi_task.starting", base_url=settings.kalshi_base_url)

    def _on_alive() -> None:
        agent.feed_health.record_tick(DataSource.KALSHI)

    while not stop_event.is_set():
        try:
            feed = KalshiFeed(
                base_url=settings.kalshi_base_url,
                key_path=settings.kalshi_private_key_path,
                key_id=settings.kalshi_api_key_id,
                on_alive=_on_alive,
                recorder=recorder,
            )
            async with feed:
                async for tick in feed.ticks():
                    if stop_event.is_set():
                        return
                    agent.ingest_pm_tick(tick)
        except Exception as exc:
            if stop_event.is_set():
                return
            log.warning("kalshi_task.reconnecting", error=str(exc))
            await asyncio.sleep(5.0)
    log.info("kalshi_task.stopped")


async def _polymarket_task(
    agent: Agent,
    stop_event: asyncio.Event,
    *,
    recorder: FrameRecorder | None = None,
) -> None:
    """Polymarket public REST market-data feed task — Round 7b / Issue 5.

    Public data only — no credentials.  US trading is geoblocked and
    OrderManager already short-circuits any signal targeting Polymarket;
    this task feeds the matcher's data side only.

    Routes every successful HTTP response into ``feed_health.record_tick``
    via the feed's ``on_alive`` callback (same liveness pattern as Kalshi),
    so the dashboard shows OK even when the BTC market universe is
    momentarily empty.
    """
    log.info(
        "polymarket_task.starting",
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
    )

    def _on_alive() -> None:
        agent.feed_health.record_tick(DataSource.POLYMARKET)

    while not stop_event.is_set():
        try:
            feed = PolymarketFeed(
                gamma_url=settings.polymarket_gamma_url,
                clob_url=settings.polymarket_clob_url,
                on_alive=_on_alive,
                recorder=recorder,
            )
            async with feed:
                async for tick in feed.ticks():
                    if stop_event.is_set():
                        return
                    agent.ingest_pm_tick(tick)
        except Exception as exc:
            if stop_event.is_set():
                return
            log.warning("polymarket_task.reconnecting", error=str(exc))
            await asyncio.sleep(5.0)
    log.info("polymarket_task.stopped")


async def _scan_task(agent: Agent, stop_event: asyncio.Event) -> None:
    log.info("scan_task.starting", interval_secs=_SCAN_INTERVAL_SECS)
    while not stop_event.is_set():
        try:
            await asyncio.sleep(_SCAN_INTERVAL_SECS)
            if stop_event.is_set():
                break

            # Check pause flag
            async with agent.shared_state.read() as s:
                if s.paused:
                    continue

            dirty = agent.flush_ticks()
            if dirty:
                agent.update_cache_from_surface(dirty)

            # Drain PM-tick buffer and run the matcher → edge → filter →
            # confidence pipeline.  Round 8 Commit 3 lifted the
            # dry-observation gate: passing signals now flow through
            # OrderManager.place + paper-trading record/simulate inside
            # run_scan_pipeline.  Async because place() is async.
            pm_ticks = agent.flush_pm_ticks()
            await agent.run_scan_pipeline(pm_ticks)

            # Mark all open paper positions against the latest ticks.  Per
            # the freshness-of-observation contract from Commit 1:
            # last_mark_at bumps on every observation, even when the side
            # mid is unchanged or the book is one-sided.
            agent.paper_positions.mark_to_market(pm_ticks)

            # Push state update to dashboard
            await agent._push_state_update()

        except Exception as exc:
            log.error("scan_task.error", error=str(exc))

    log.info("scan_task.stopped")


async def _order_refresh_task(agent: Agent, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(_ORDER_REFRESH_SECS)
        if stop_event.is_set():
            break
        try:
            await agent.order_mgr.refresh_all()
            for o in agent.order_mgr.filled_orders():
                agent.tracker.record_fill(o)
        except Exception as exc:
            log.error("order_refresh.error", error=str(exc))


async def _paper_settlement_task(agent: Agent, stop_event: asyncio.Event) -> None:
    """Run the Kalshi paper-settlement poller (Commit 2) until stop_event fires.

    The poller's own ``run`` loop owns its 60-second cadence and
    error-recovery; this task is just a thin async-context wrapper so
    ``aclose`` runs on shutdown for any HTTP client the poller
    constructed.
    """
    try:
        await agent.paper_settlement_poller.run(stop_event)
    finally:
        await agent.paper_settlement_poller.aclose()


async def _dashboard_task(agent: Agent, stop_event: asyncio.Event) -> None:
    """Run the FastAPI dashboard server as a uvicorn task."""
    fastapi_app = create_app(shared_state=agent.shared_state)
    config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=_DASHBOARD_PORT,
        log_level="warning",
        loop="none",   # reuse existing event loop
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # don't hijack our signals
    log.info("dashboard.starting", port=_DASHBOARD_PORT)
    try:
        await server.serve()
    except Exception as exc:
        log.error("dashboard.error", error=str(exc))


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def run(
    dry_run: bool = True, record_dir: Path | None = None,
) -> None:
    _configure_logging()

    # Round 9c Commit 2: optional raw-feed recorder.  When ``record_dir``
    # is set (driven by ``--record-feeds`` on the CLI) every feed task
    # is wired to record raw frames into a per-source / per-day / per-hour
    # gzipped JSONL stream for future replay-mode validation.  Off by
    # default — recording is opt-in.
    recorder: FrameRecorder | None = None
    if record_dir is not None:
        recorder = FrameRecorder(record_dir)
        log.info("frame_recorder.enabled", base_dir=str(record_dir))

    agent = Agent(dry_run=dry_run)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows: loop.add_signal_handler is not supported.
            # signal.signal works for SIGINT; SIGTERM is best-effort.
            try:
                signal.signal(sig, lambda *_: stop_event.set())
            except (ValueError, OSError):
                pass

    log.info(
        "agent.starting",
        dry_run=dry_run,
        min_edge=settings.min_edge,
        max_position_usd=settings.max_position_usd,
    )

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                _deribit_task(agent, stop_event, recorder=recorder),
                name="deribit-feed",
            )
            tg.create_task(
                _kalshi_task(agent, stop_event, recorder=recorder),
                name="kalshi-feed",
            )
            tg.create_task(
                _polymarket_task(agent, stop_event, recorder=recorder),
                name="polymarket-feed",
            )
            tg.create_task(_scan_task(agent, stop_event), name="scan")
            tg.create_task(_order_refresh_task(agent, stop_event), name="order-refresh")
            tg.create_task(agent.settlement_monitor.run(stop_event), name="settlement")
            tg.create_task(
                _paper_settlement_task(agent, stop_event), name="paper-settlement",
            )
            tg.create_task(_dashboard_task(agent, stop_event), name="dashboard")
    except* KeyboardInterrupt:
        stop_event.set()
    except* Exception as eg:
        for exc in eg.exceptions:
            log.error("agent.task_error", error=str(exc))
        stop_event.set()
    finally:
        await agent.order_mgr.aclose()
        if recorder is not None:
            recorder.close()
        summary = agent.tracker.performance_summary()
        log.info("agent.shutdown", **summary)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="btc_pm_arb",
        description=(
            "BTC prediction-market <-> Deribit options arbitrage agent. "
            "Runs in dry-run / paper-trading mode by default."
        ),
    )
    # Round 9c Commit 2: opt-in raw-feed recording.  --dry-run is NOT
    # exposed as a CLI flag — agent dry_run state is governed by
    # config / standing orders, not per-invocation.
    parser.add_argument(
        "--record-feeds", action="store_true",
        help=(
            "Record raw frames from all feeds into --record-dir for "
            "future replay-mode validation.  Off by default."
        ),
    )
    parser.add_argument(
        "--record-dir", type=Path, default=Path("data/recordings"),
        help="Base directory for raw-frame recordings (default: data/recordings).",
    )
    args = parser.parse_args()
    record_dir = args.record_dir if args.record_feeds else None
    asyncio.run(run(dry_run=True, record_dir=record_dir))


if __name__ == "__main__":
    main()
