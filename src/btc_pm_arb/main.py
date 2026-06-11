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
import shutil
import signal
import sys
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine

import structlog
import uvicorn

from btc_pm_arb.clock import SimulatedClock
from btc_pm_arb.config import settings
from btc_pm_arb.execution.benchmark_settlement import PaperBenchmarkSettler
from btc_pm_arb.execution.fill_simulator import (
    BookSnapshot,
    FillEvaluation,
    FillSimulator,
)
from btc_pm_arb.execution.orders import OrderManager, Order
from btc_pm_arb.execution.paper_ledger import (
    BookLevel,
    PaperIntentRecord,
    PaperLedger,
    PaperNoarbShadowRecord,
    PaperOrderRecord,
    PaperRejectionRecord,
    PaperRiskBlockRecord,
)
from btc_pm_arb.execution.paper_positions import PaperPosition, PaperPositionTracker
from btc_pm_arb.execution.paper_settlement import KalshiSettlementPoller
from btc_pm_arb.execution.positions import PositionTracker
from btc_pm_arb.execution.risk import RiskConfig, RiskManager
from btc_pm_arb.execution.risk_limits import (
    RiskIntent,
    RiskLimits,
    build_portfolio_state,
    check_risk,
)
from btc_pm_arb.execution.settlement import SettlementMonitor
from btc_pm_arb.feeds.aux_capture import (
    ChainlinkRoundCapture,
    DeribitIndexCapture,
    Polymarket5MinCapture,
)
from btc_pm_arb.feeds.deribit import DeribitFeed
from btc_pm_arb.feeds.discovery import MarketDiscovery, run_discovery_loop
from btc_pm_arb.feeds.health import FeedHealthTracker
from btc_pm_arb.feeds.kalshi import KalshiFeed
from btc_pm_arb.feeds.polymarket import PolymarketFeed
from btc_pm_arb.feeds.recorder import (
    FrameRecorder,
    configure_recorder_file_log,
)
from btc_pm_arb.feeds.watchdog import RecorderWatchdog
from btc_pm_arb.models import ArbitrageSignal, DataSource, OptionTick, PredictionMarketTick
from btc_pm_arb.pricing.cache import ProbabilityCache
from btc_pm_arb.pricing.digital_pricer import DigitalPricer
from btc_pm_arb.pricing.noarb import noarb_check_by_strike
from btc_pm_arb.pricing.realized_vol import RealizedVolTracker
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.server.app import create_app
from btc_pm_arb.server.state import SharedState
from btc_pm_arb.signals.confidence import ConfidenceScorer
from btc_pm_arb.signals.edge import (
    EdgeCalculator,
    EdgeResult,
    fill_adjusted_price,
    model_yes_bounds,
)
from btc_pm_arb.signals.filters import FilterConfig, SignalFilter, _extract_reason_key
from btc_pm_arb.signals.matcher import ContractMatcher
from btc_pm_arb.signals.velocity import OddsVelocityTracker

log: structlog.BoundLogger = structlog.get_logger("main")

_SCAN_INTERVAL_SECS: float = 5.0
_ORDER_REFRESH_SECS: float = 30.0
_BASE_SIZE_USD: float = 200.0
_DASHBOARD_PORT: int = 8000

# Rejection-path shadow fill (measurement infra): the reason_key buckets whose
# rejections carry a meaningful would-be fill -- edge-economics gates where the
# contract had a positive best_side and a book to walk but the edge fell below
# (or near) the floor.  Structural rejections (no_positive_edge, range /
# one_touch product, empty_book, stale, expiry, spread, match_quality, feed
# staleness, ...) have no meaningful fill and are skipped.
_SHADOW_FILL_REASON_KEYS: frozenset[str] = frozenset(
    {"conservative_edge", "mid_edge", "regime_adjusted_edge"}
)
# Conservative-edge floor below which a rejection is pipeline noise not worth
# book-walking (mirrors the goal's reason_key-in-set AND edge >= ~0.005 filter).
_SHADOW_FILL_MIN_EDGE: float = 0.005


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

    def __init__(
        self,
        dry_run: bool = True,
        clock: SimulatedClock | None = None,
        run_id: str | None = None,
    ) -> None:
        self.dry_run = dry_run
        # Build step 4: a per-run id stamped onto every ledger append, so
        # live vs replay runs (and successive runs) are separable and
        # joinable.  Generated per process if not supplied; goal-2 replay
        # will pass a deterministic id so a re-run reproduces the ledger.
        self.run_id = run_id or uuid.uuid4().hex
        # Simulated-clock seam (build step 1, Fork 3).  Defaults to a
        # live clock that delegates to datetime.now(timezone.utc), so a
        # default Agent() behaves exactly as before.  In replay mode the
        # orchestrator injects a SimulatedClock("replay") that the replay
        # reader (build step 5 — separate follow-up) advances off the
        # recorded "ts" stream.  Threaded into the freshness-sensitive
        # call sites below: FeedHealthTracker, the SignalFilter freshness
        # gates (via run_scan_pipeline), and the settlement poller.
        self.clock = clock or SimulatedClock("live")
        self.surface = VolSurface()
        self.cache = ProbabilityCache()
        self.pricer = DigitalPricer()
        # Sim-clock seam (C1): under replay the rv tracker must classify the
        # vol regime from sim-time, not wall-clock — an as-fast-as-possible
        # replay otherwise stamps every index sample ~1 wall-second apart and
        # annualizes a phantom HIGH regime (live reads LOW on identical
        # frames).  Live: clock.now() == datetime.now(utc), so unchanged.
        self.rv_tracker = RealizedVolTracker(clock=self.clock)
        self.feed_health = FeedHealthTracker(clock=self.clock)
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
        # replay_mode lets the dashboard label the run paper / live / replay.
        # Derived from the injected clock so it follows the orchestrator's
        # --mode without a second source of truth.
        self.shared_state = SharedState(
            dry_run=dry_run,
            live_mode_token=live_token,
            replay_mode=(self.clock.mode == "replay"),
        )

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
        # Build step 4: stamp run_id + mode (from the clock) on every append.
        self.paper_ledger = PaperLedger(
            settings.paper_ledger_dir, run_id=self.run_id, mode=self.clock.mode,
        )
        # Build step 2 (Fork 1): the paper FillSimulator walks captured
        # book depth and produces partial fills / explicit no_fills, rather
        # than the legacy top-of-book full-or-nothing.  PM and Kalshi share
        # this one instance.
        self.fill_simulator = FillSimulator(book_walk=True)
        self.paper_positions = PaperPositionTracker()
        # Risk-limit layer (risk-limit goal): declarative caps evaluated in
        # run_scan_pipeline AFTER all edge/confidence gates and BEFORE
        # OrderManager.place builds an order.  Caps are checked against
        # run_id-scoped event-sourced state read back from the ledger files
        # (NOT self.paper_positions, which is rehydrated below from ALL runs'
        # records and is therefore cross-run contaminated -- see
        # docs/diag_risklimits.md).  The reader is a dedicated instance so
        # per-intent replays don't inflate self.paper_ledger's
        # load-verification counters (health() semantics unchanged).
        self.risk_limits = RiskLimits()
        self._risk_ledger_reader = PaperLedger(settings.paper_ledger_dir)
        # Order registry keyed by client_order_id — populated as the agent
        # places paper orders, and re-populated from disk on startup so the
        # settlement poller can still look up theoretical_edge for positions
        # opened in a previous process lifetime.
        self._paper_orders_by_id: dict[str, PaperOrderRecord] = {}
        # No-arb shadow layer (no-arb goal Phase 2): latest static no-arb
        # reasons per (strike, expiry) grid point, written/cleared at the
        # digital pricing site in lockstep with each cache write (the flag
        # and the entry the matcher later consumes come from the SAME fit;
        # see docs/diag_noarb.md section 5).  Read-only downstream: the
        # edge step appends a noarb_shadow record per flagged edge and
        # NOTHING else reads this -- the filter/place path is untouched.
        self._noarb_flags: dict[tuple[float, datetime], list[str]] = {}
        # Signal-funnel counters surfaced via SharedState.signal_fired_skipped.
        # All increments happen from the scan task (via run_scan_pipeline);
        # serialized by the single event loop, no locking required.
        self._funnel: dict[str, int] = {
            "signals_observed_total": 0,
            "signals_passed_filter": 0,
            "signals_rejected_filter": 0,
            "paper_orders_placed": 0,
            # Risk-limit layer: intents that passed every edge/confidence
            # gate but breached a declarative cap (risk_limits.check_risk)
            # -- no order placed, one risk_block record appended each.
            "paper_orders_risk_blocked": 0,
            "paper_orders_filled": 0,
            # Build step 2: book-walking can fill only part of size_usd when
            # the crossed book is thin -- counted separately from full fills
            # and no_fills so the funnel never silently mislabels a partial.
            "paper_orders_partial": 0,
            "paper_orders_no_fill": 0,
            # Round 9 Commit 9a: counter for the defensive
            # paper_ledger.missing_originating_data warning at the place()
            # → record() handoff in run_scan_pipeline.  Should always
            # remain 0 in healthy operation; non-zero indicates a
            # passing-signal contract_id that wasn't in the
            # edges_by_id / tick_by_contract lookups (a bug worth seeing).
            "paper_ledger_missing_originating_data": 0,
            # Rejection-path shadow fill (measurement infra): how many
            # near-floor rejections got a book-walked fill-adjusted edge, and
            # how many were skipped because a malformed recorded book made the
            # walk raise (additive path -- never disturbs the rejection
            # decision; a non-zero error count is worth seeing).
            "shadow_fill_walked": 0,
            "shadow_fill_errors": 0,
            # Rejection-path chase fill (#1, UNCAPPED completion cost): how many
            # of the SAME in-band near-floor rejections also carry a
            # chase-adjusted edge (book walked to completion through the wall),
            # and how many were skipped because the uncapped walk raised.
            # Additive measurement path -- never disturbs the rejection decision.
            "chase_fill_walked": 0,
            "chase_fill_errors": 0,
            # No-arb shadow layer: edges computed on a fit that violated
            # static no-arb at the digital pricing site -- one noarb_shadow
            # record appended each.  Observe-only; never disturbs the
            # filter/place path.
            "noarb_shadow_flagged": 0,
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
            clock=self.clock,
        )

        # Build step 3: deterministic benchmark settlement for PM paper
        # positions (Fork 2).  No HTTP / no live oracle: at expiry it reads
        # the benchmark BTC fixing from self._benchmark_btc_price and
        # evaluates a terminal-digital model, so a replay reproduces the
        # settlement from the recorded surface alone.  Driven once per scan
        # tick (see _scan_task) through the same sim-clock seam.
        self.paper_benchmark_settler = PaperBenchmarkSettler(
            tracker=self.paper_positions,
            ledger=self.paper_ledger,
            get_order_record=lambda cid: self._paper_orders_by_id.get(cid),
            benchmark_price_fn=self._benchmark_btc_price,
            clock=self.clock,
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
        # Measure time-to-expiry against the shared clock (sim-time under
        # replay) so the SVI fit is deterministic and reads the recorded
        # timeline, not the wall-clock instant the replay runs at.  Live:
        # clock.now() == datetime.now(utc), so behaviour is unchanged.
        dirty = self.surface.update(self._pending_ticks, now=self.clock.now())
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
            # No-arb shadow layer (observe-only): static no-arb state of THIS
            # fit at every strike about to be priced.  Flags are set/cleared
            # below in lockstep with each cache write, so the flag for a
            # (strike, expiry) always describes the fit that produced the
            # cache entry the matcher later consumes.  Nothing on the pricing
            # or signal path reads the result -- price/cache writes are
            # byte-identical with or without it.
            noarb_by_strike = noarb_check_by_strike(
                self.surface, sorted(strikes), expiry,
            )
            for strike in strikes:
                price = self.pricer.price_from_surface(strike, expiry, self.surface)
                if price is None:
                    continue
                reasons = noarb_by_strike.get(strike)
                if reasons:
                    self._noarb_flags[(strike, expiry)] = reasons
                else:
                    self._noarb_flags.pop((strike, expiry), None)
                self.cache.update(
                    strike=strike,
                    expiry=expiry,
                    bid_prob=price.bid,
                    ask_prob=price.ask,
                    mid_prob=price.mid,
                    source=DataSource.DERIBIT,
                    # Sim-time under replay so the cache entry's freshness
                    # (read by the stale-data gate) tracks the recorded
                    # timeline; wall-clock in live (unchanged).
                    timestamp=self.clock.now(),
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
            clock=self.clock,
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
                clock=self.clock,
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
            _reason_key = _extract_reason_key(_reason)
            # Measurement infra (additive): near-floor edge-economics
            # rejections get a fill-adjusted edge via the SAME book-walk placed
            # orders use, so the [1-3%) band carries an honest depth-aware fill
            # estimate instead of only the 1-2 contracts that clear the floor.
            # The contract STAYS rejected -- nothing here changes the gate
            # outcome.  None for rejections outside the band (no meaningful
            # fill).
            _fae, _evaluation = self._shadow_fill_for_rejection(_edge, _reason_key)
            # #1 (UNCAPPED chase) rides ALONGSIDE #2 (capped passive fill) on
            # the IDENTICAL in-band set: ``_evaluation is not None`` is exactly
            # "the rejection passed the #2 filter and the book-walk did not
            # raise", so #1 is computed for the same near-floor rejections #2 is
            # -- never independently re-filtered, so the two can never drift
            # apart.  #1 is the primary fill-adjusted edge for the Jun 5 capture
            # (it CAN go negative, exposing correctly-rejected losers); #2 is
            # untouched.
            _chase: float | None = None
            if _evaluation is not None:
                self._funnel["shadow_fill_walked"] += 1
                _chase = self._chase_adjusted_edge_for_rejection(_edge)
                if _chase is not None:
                    self._funnel["chase_fill_walked"] += 1
            self.paper_ledger.append_rejection(
                PaperRejectionRecord(
                    timestamp=_edge.timestamp,
                    contract_id=_edge.match.pm_tick.contract_id,
                    platform=_edge.match.pm_tick.source,
                    reason_key=_reason_key,
                    full_reason=_reason,
                    best_conservative_edge=_edge.best_conservative_edge,
                    vol_regime=self.rv_tracker.current_regime().value,
                    fill_adjusted_edge=_fae,
                    fill_simulator_reason=(
                        _evaluation.reason if _evaluation is not None else None
                    ),
                    fill_outcome=(
                        _evaluation.outcome if _evaluation is not None else None
                    ),
                    fill_size_usd=(
                        _evaluation.fill_size_usd if _evaluation is not None else None
                    ),
                    chase_adjusted_edge=_chase,
                )
            )

        # ── No-arb shadow layer (no-arb goal Phase 2; observe-only) ──────────
        # One noarb_shadow record per edge computed THIS scan whose consumed
        # (matched_strike, matched_expiry) grid point was flagged at the
        # digital pricing site -- the would_reject a future suppression layer
        # WOULD have made, with the would-be edge it was carrying.  STRICTLY
        # additive: nothing here reads back into the filter/place path;
        # ``passing`` and every emitted signal are untouched (Phase 3 pins
        # this with a before/after banked-replay identity check).  Timestamp
        # is clock.now() (sim-time under replay) so the stream is
        # deterministic on banked data.
        for _edge in edges:
            _noarb_reasons = self._noarb_flags.get(
                (_edge.match.matched_strike, _edge.match.matched_expiry)
            )
            if not _noarb_reasons:
                continue
            self._funnel["noarb_shadow_flagged"] += 1
            self.paper_ledger.append_noarb_shadow(
                PaperNoarbShadowRecord(
                    timestamp=self.clock.now(),
                    contract_id=_edge.match.pm_tick.contract_id,
                    platform=_edge.match.pm_tick.source,
                    reasons=list(_noarb_reasons),
                    strike=_edge.match.matched_strike,
                    expiry=_edge.match.matched_expiry,
                    best_side=_edge.best_side,
                    best_conservative_edge=_edge.best_conservative_edge,
                    fill_adjusted_edge=_edge.fill_adjusted_edge,
                    signal_emitted=(
                        _edge.match.pm_tick.contract_id in passing_ids
                    ),
                )
            )

        self._latest_signals = self._build_signal_payloads(
            passing, rejected, edges_by_id,
        )

        # Risk-limit layer: intents already risk-blocked THIS scan.  The
        # drained 5 s tick buffer can hold several ticks for one contract,
        # and batch_match does not dedupe, so ``passing`` can carry the
        # same (contract, side) intent more than once per scan.  On the
        # allow path place()'s fingerprint collapses the duplicates; this
        # set is the block-path counterpart, keeping "exactly one
        # risk_block record per blocked intent" true at scan granularity.
        _risk_blocked_this_scan: set[tuple[str, str]] = set()

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
                # -- Risk-limit layer (risk-limit goal) ----------------------
                # Declarative caps, evaluated strictly AFTER every edge/
                # confidence gate above and strictly BEFORE place() builds an
                # order.  Pre-place blocking is load-bearing: place()
                # registers the dedupe fingerprint, so blocking after it
                # would suppress the signal forever even once headroom
                # returns; blocking before it leaves the intent free to
                # re-evaluate next scan.  Gated on is_duplicate so a signal
                # place() would dedupe anyway never reaches the cap check
                # (no spurious risk_block records for already-placed
                # signals).  On block: NO order, exactly one run_id-stamped
                # risk_block record with the reason -- a block is a return
                # value, never an exception.
                _risk_key = (sig.pm_quote.contract_id, sig.trade_side)
                if _risk_key in _risk_blocked_this_scan:
                    # Duplicate of an intent already blocked this scan
                    # (duplicate ticks for one contract in the buffer):
                    # same decision, no second record, and no order.
                    continue
                if not self.order_mgr.is_duplicate(sig):
                    _risk_intent = RiskIntent(
                        platform=sig.pm_quote.source,
                        contract_id=sig.pm_quote.contract_id,
                        # IDENTICAL side derivation to OrderManager.place
                        # (orders.py): a buy_yes is a YES order.
                        side="yes" if sig.trade_side == "buy_yes" else "no",
                        size_usd=_BASE_SIZE_USD,
                    )
                    _risk_now = self.clock.now()
                    _pstate = build_portfolio_state(
                        self._risk_ledger_reader,
                        run_id=self.run_id,
                        platform=_risk_intent.platform,
                        contract_id=_risk_intent.contract_id,
                        today=_risk_now.date(),
                    )
                    _allowed, _block_reason = check_risk(
                        _risk_intent, _pstate, self.risk_limits,
                    )
                    if not _allowed:
                        _risk_blocked_this_scan.add(_risk_key)
                        # Append before counting so the funnel can never
                        # claim a block whose record failed to persist.
                        self.paper_ledger.append_risk_block(
                            PaperRiskBlockRecord(
                                timestamp=_risk_now,
                                platform=_risk_intent.platform,
                                contract_id=_risk_intent.contract_id,
                                side=_risk_intent.side,
                                size_usd=_risk_intent.size_usd,
                                reason=_block_reason,
                                market_position_usd=_pstate.market_position_usd,
                                global_exposure_usd=_pstate.global_exposure_usd,
                                daily_realized_pnl_usd=(
                                    _pstate.daily_realized_pnl_usd
                                ),
                            )
                        )
                        self._funnel["paper_orders_risk_blocked"] += 1
                        log.info(
                            "risk.blocked",
                            contract=_risk_intent.contract_id,
                            platform=_risk_intent.platform.value,
                            side=_risk_intent.side,
                            size_usd=_risk_intent.size_usd,
                            reason=_block_reason,
                        )
                        continue
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

    def _shadow_fill_for_rejection(
        self, edge: EdgeResult, reason_key: str,
    ) -> tuple[float | None, FillEvaluation | None]:
        """Compute the would-be fill-adjusted edge for a near-floor rejection.

        Measurement infra (additive): the contract STAYS rejected — this only
        records what its fill WOULD have been, so the near-floor band carries a
        depth-aware fill estimate instead of only the 1-2 contracts that clear
        the floor.  Reuses the SAME ``self.fill_simulator`` book-walk and the
        SAME side / limit_price / size derivation as the placed-order path
        (``OrderManager.place`` orders.py:446-449 + ``_record_paper_order``);
        it is NOT a second, more optimistic fill model.

        Returns ``(None, None)`` for rejections outside the edge-economics band
        (reason_key not in the set, no positive ``best_side``, or sub-noise
        edge) — no meaningful fill.  For a walked rejection returns
        ``(fill_adjusted_edge, evaluation)`` where ``fill_adjusted_edge`` is the
        model fair value minus the book-walked fill price, or ``None`` when the
        walk did not fill (empty / limit-below book) — never a manufactured
        positive edge.
        """
        if (
            reason_key not in _SHADOW_FILL_REASON_KEYS
            or edge.best_side is None
            or edge.best_conservative_edge < _SHADOW_FILL_MIN_EDGE
        ):
            return None, None
        try:
            side = "yes" if edge.best_side == "buy_yes" else "no"
            pm = edge.match.pm_quote
            # IDENTICAL limit-price derivation to OrderManager.place
            # (orders.py:446-449): a buy_yes lifts the YES ask; a buy_no lifts
            # the implied NO ask (1 - YES bid).
            limit_price = pm.ask_prob if side == "yes" else (1.0 - pm.bid_prob)
            snapshot = BookSnapshot.from_tick(edge.match.pm_tick)
            evaluation = self.fill_simulator.evaluate(
                side=side,  # type: ignore[arg-type]
                limit_price=limit_price,
                size_usd=_BASE_SIZE_USD,
                snapshot=snapshot,
            )
        except Exception:
            # Additive measurement path must never disturb the scan pipeline or
            # the (unchanged) rejection decision.  A malformed recorded book
            # yields no shadow fill; deterministic given the recorded inputs.
            self._funnel["shadow_fill_errors"] += 1
            log.warning(
                "shadow_fill.error",
                contract_id=edge.match.pm_tick.contract_id,
                reason_key=reason_key,
            )
            return None, None
        if evaluation.fill_price is None:
            # Honest no-fill (empty book / limit below the whole book): record
            # the simulator reason, but no fill-adjusted edge — do NOT invent
            # one against an empty near-mid book.
            return None, evaluation
        # Fair value mirrors edge._compute_fill_adjusted_edge: raw model-YES
        # lower bound for a buy_yes, (1 - upper bound) for a buy_no.  Only the
        # fill PRICE differs from the passing-order path — and that price comes
        # from the same honest book-walk.
        my_bid, my_ask = model_yes_bounds(edge.match)
        fair = my_bid if side == "yes" else (1.0 - my_ask)
        return fair - evaluation.fill_price, evaluation

    def _chase_adjusted_edge_for_rejection(
        self, edge: EdgeResult,
    ) -> float | None:
        """Compute the UNCAPPED 'chase-into-the-wall' fill-adjusted edge (#1).

        Companion to ``_shadow_fill_for_rejection`` (#2), NOT a replacement.
        Called ONLY for the in-band near-floor set #2 already accepted (the
        caller gates on ``_shadow_fill_for_rejection`` returning an evaluation),
        so #1 and #2 measure the IDENTICAL rejections.

        Where #2 caps the book-walk at the limit (passive fill: never crosses
        the price, never negative, a thin book shows up as a partial-fill rate),
        #1 reuses the SAME uncapped book-walker placed orders use --
        ``signals.edge.fill_adjusted_price`` -- which walks the WHOLE book to
        COMPLETE the full size, crossing the 0.99 wall.  The side / size
        derivation is identical to #2 (``buy_yes`` -> the YES book, ``buy_no``
        -> the NO book; ``_BASE_SIZE_USD``); only the cap differs.  The fair
        value is the same ``model_yes_bounds`` value #2 uses, so #1 and #2 share
        a fair value and differ ONLY in fill price -- exactly the relationship
        between ``orders.jsonl``'s fill_adjusted_edge and a placed order.

        Returns the chase-adjusted edge ``fair - completion_vwap``.  Because the
        walk chases through the wall to fill, this CAN BE NEGATIVE; the negative
        is returned AS-IS and never clipped at zero -- a negative result is a
        CORRECTLY-REJECTED LOSER (completing the contract loses money), not a
        missed signal.  Returns ``None`` for an honest no-fill (whole book too
        thin to complete the size) -- never a manufactured edge.
        """
        try:
            side = "yes" if edge.best_side == "buy_yes" else "no"
            # IDENTICAL side->book mapping as edge._compute_fill_adjusted_edge:
            # a buy_yes walks the YES book, a buy_no walks the NO book.
            book = (
                edge.match.pm_tick.order_book_yes
                if side == "yes"
                else edge.match.pm_tick.order_book_no
            )
            # UNCAPPED: fill_adjusted_price walks every level to fill the full
            # size (returns None only if the whole book cannot) -- it does NOT
            # stop at the limit, so the 0.99 wall drags the completion VWAP up.
            chase_fill_price = fill_adjusted_price(book, size_usd=_BASE_SIZE_USD)
        except Exception:
            # Additive measurement path: a malformed recorded book must never
            # disturb the (unchanged) rejection decision -- yields no chase edge.
            self._funnel["chase_fill_errors"] += 1
            log.warning(
                "chase_fill.error",
                contract_id=edge.match.pm_tick.contract_id,
            )
            return None
        if chase_fill_price is None:
            # Honest no-fill: the whole book is too thin to complete the size.
            # Do NOT invent an edge.
            return None
        my_bid, my_ask = model_yes_bounds(edge.match)
        fair = my_bid if side == "yes" else (1.0 - my_ask)
        # Returned as-is: a negative completion cost is the POINT (a
        # correctly-rejected loser), never clipped to look better.
        return fair - chase_fill_price

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

        # Build step 6 (plan 3.6; criterion 2): emit the shadow no-op order
        # intent the LIVE routing path would have submitted (venue / side /
        # limit / size + the top-of-book snapshot it was formed against)
        # BEFORE the FillSimulator evaluates -- submitting nothing.  This is
        # the audit trail live execution WOULD have produced, captured without
        # execution; submitted=False marks the no-op.
        self.paper_ledger.append_intent(
            PaperIntentRecord(
                client_order_id=order.client_order_id,
                created_at=order.created_at,
                platform=order.platform,
                contract_id=order.contract_id,
                side=order.side,  # type: ignore[arg-type]
                size_usd=order.size_usd,
                limit_price=order.limit_price,
                pm_yes_bid=tick.yes_bid,
                pm_yes_ask=tick.yes_ask,
                pm_no_bid=tick.no_bid,
                pm_no_ask=tick.no_ask,
                submitted=False,
            )
        )

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
        elif evaluation.outcome == "partial":
            self._funnel["paper_orders_partial"] += 1
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
            # Build step 3: capture the contract threshold + polarity so the
            # benchmark settler can evaluate the terminal-digital predicate
            # deterministically at expiry without a live oracle call.
            strike=signal.pm_quote.strike,
            direction=signal.pm_quote.direction,
            dry_run=self.dry_run,
        )

    def _benchmark_btc_price(self, expiry: datetime) -> float | None:
        """Deterministic benchmark BTC fixing for PM settlement (build step 3).

        v1 uses the latest observed Deribit index price (read the same way
        the dashboard payload does, via the RV tracker's log-price series).
        Deterministic given the recorded surface, so a replay reproduces it
        with no live call.  The ``expiry`` argument is accepted for a future
        expiry-nearest fixing; v1 ignores it and uses the latest observed
        index (Carried Gap a: a basis-like sim/real wedge vs. the real PM
        oracle).  Returns None when no index has been observed yet.
        """
        import math

        if self.rv_tracker.n_points <= 0:
            return None
        return math.exp(self.rv_tracker._data[-1][1])

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


# All six capture streams recorded under --record-feeds, watched by the
# RecorderWatchdog (2026-06-10 silent-stop incident).
_CAPTURE_STREAMS: tuple[str, ...] = (
    "deribit", "kalshi", "polymarket", "spot", "chainlink", "pm5min",
)


def _supervised_task(
    tg: asyncio.TaskGroup,
    name: str,
    factory: Callable[[], Coroutine[Any, Any, None]],
) -> Callable[[], bool]:
    """Create a named task and return a restart hook for the watchdog.

    The hook re-creates the task from ``factory`` only when the previous
    task has finished -- a live task is never duplicated (a stream can
    be silent while its task is alive, e.g. upstream quiet; restarting
    would double-connect).  Returns True when a restart was initiated.
    """
    holder: dict[str, asyncio.Task] = {}

    def _start() -> None:
        holder["task"] = tg.create_task(factory(), name=name)

    def _restart() -> bool:
        task = holder.get("task")
        if task is not None and not task.done():
            return False
        try:
            _start()
        except RuntimeError:
            # TaskGroup already winding down -- nothing to restart into.
            return False
        return True

    _start()
    return _restart


def _start_aux_capture_tasks(
    tg: asyncio.TaskGroup,
    stop_event: asyncio.Event,
    recorder: FrameRecorder,
) -> dict[str, Callable[[], bool]]:
    """Start the capture-only auxiliary latency-analysis streams.

    Called ONLY in live mode under ``--record-feeds`` (``recorder`` non-None).
    These three streams (fast spot, Chainlink round state, PM 5-minute odds)
    are recorded for offline latency analysis and feed NOTHING into pricing,
    signals, gates, or execution -- see ``feeds/aux_capture.py``.  Each task's
    own run loop is resilient (swallows its errors, self-disables a blocked
    stream) so it can never raise into the TaskGroup or disturb the live feeds.
    The default capture (deribit / kalshi / polymarket) is byte-unchanged
    whether or not these run.

    Returns the per-stream restart hooks for the RecorderWatchdog (a
    self-disabled aux stream is the prime supervised-restart target).
    """
    spot = DeribitIndexCapture(recorder, url=settings.deribit_url)
    chainlink = ChainlinkRoundCapture(
        recorder,
        rpc_url=settings.chainlink_polygon_rpc_url,
        feed_address=settings.chainlink_btc_usd_feed,
    )
    pm5min = Polymarket5MinCapture(
        recorder,
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
    )
    restarters = {
        "spot": _supervised_task(
            tg, "aux-spot", lambda: spot.run(stop_event),
        ),
        "chainlink": _supervised_task(
            tg, "aux-chainlink", lambda: chainlink.run(stop_event),
        ),
        "pm5min": _supervised_task(
            tg, "aux-pm5min", lambda: pm5min.run(stop_event),
        ),
    }
    log.info("aux_capture.enabled", streams=["spot", "chainlink", "pm5min"])
    return restarters


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

            # Build step 3: settle any PM paper positions whose expiry has
            # passed (sim-clock) against the deterministic benchmark model.
            # No-op until a position reaches expiry, so short live runs are
            # unaffected; Kalshi keeps settling via its own poller task.
            agent.paper_benchmark_settler.settle_due()

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


async def _duration_task(
    stop_event: asyncio.Event, duration_s: float,
) -> None:
    """Bound the run: set ``stop_event`` after ``duration_s`` seconds.

    Build step 1 (``--duration N``) so a run is CI-bounded.  In live mode
    this is wall-clock seconds; under replay the bound is on simulated
    time advanced by the replay reader (build step 5) — that reader will
    own its own stop condition, so this wall-clock fallback only matters
    for the live path here.  Cancelled cleanly when the run stops for
    another reason (signal / task error).
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=duration_s)
    except asyncio.TimeoutError:
        log.info("run.duration_reached", duration_s=duration_s)
        stop_event.set()


async def _dashboard_task(agent: Agent, stop_event: asyncio.Event) -> None:
    """Run the FastAPI dashboard server as a uvicorn task.

    Honors ``stop_event`` (2026-06-10 fix): the original implementation
    awaited ``server.serve()`` unconditionally, so the TaskGroup never
    exited -- ``--duration`` runs never completed and NO run path ever
    reached ``recorder.close()`` (the cause of the final-hour gzip
    truncation).  Now ``serve()`` races ``stop_event.wait()``; on stop,
    uvicorn is asked to exit via ``server.should_exit`` and given a
    bounded grace before a hard cancel.
    """
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
    serve_task = asyncio.create_task(server.serve(), name="dashboard-serve")
    stop_task = asyncio.create_task(stop_event.wait(), name="dashboard-stop")
    try:
        done, _pending = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if serve_task in done:
            exc = serve_task.exception()
            if exc is not None:
                log.error("dashboard.error", error=str(exc))
        else:
            # stop_event fired: ask uvicorn to exit, bounded grace.
            server.should_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("dashboard.stop_timeout_forcing_cancel")
                serve_task.cancel()
                try:
                    await serve_task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception as exc:
                log.error("dashboard.error", error=str(exc))
            log.info("dashboard.stopped")
    finally:
        for t in (serve_task, stop_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


# ── Replay (build step 5) ──────────────────────────────────────────────────────


def _latest_replay_date(base: Path) -> str | None:
    """Return the latest ``YYYY-MM-DD`` recording dir under ``base``, or None.

    Recordings live at ``base/{source}/{date}/frames-HH.jsonl.gz``; the date
    is shared across sources for a capture, so the max date across the three
    source trees is the most recent capture day.
    """
    dates: set[str] = set()
    for source in ("deribit", "polymarket", "kalshi"):
        src_dir = base / source
        if src_dir.is_dir():
            for child in src_dir.iterdir():
                if child.is_dir():
                    dates.add(child.name)
    return max(dates) if dates else None


async def _run_replay(
    agent: "Agent", record_dir: Path | None, replay_date: str | None,
) -> None:
    """Drive the agent off recorded frames via :class:`ReplayReader`.

    Build step 5.  The reader is the only clock driver in replay mode; it
    reaches no network (no feed tasks, no Kalshi settlement poller, no
    dashboard are started).
    """
    from btc_pm_arb.feeds.replay import ReplayReader

    base = record_dir if record_dir is not None else Path("data/recordings")
    date = replay_date or _latest_replay_date(base)
    if date is None:
        log.error("replay.no_recordings", base_dir=str(base))
        return
    reader = ReplayReader(record_dir=base, date=date, agent=agent)
    log.info("replay.starting", base_dir=str(base), date=date, run_id=agent.run_id)
    stats = await reader.run()
    log.info("replay.finished", **stats)


# ── Recorder preflight (2026-06-10 ENOSPC incident) ───────────────────────────


def _record_feeds_preflight(record_dir: Path) -> bool:
    """Refuse to start --record-feeds on a too-full volume.

    Free space is measured via ``shutil.disk_usage`` on the nearest
    existing ancestor of ``record_dir`` (the directory itself may not
    exist before the first frame) -- NEVER by summing file sizes.
    Floor: ``settings.recorder_min_free_gb`` (default 20 GiB).
    """
    probe = record_dir.resolve()
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    free = shutil.disk_usage(probe).free
    floor = int(settings.recorder_min_free_gb * 2**30)
    if free < floor:
        log.critical(
            "recorder.preflight_insufficient_disk",
            free_gb=round(free / 2**30, 2),
            floor_gb=settings.recorder_min_free_gb,
            record_dir=str(record_dir),
            hint="free disk space or lower recorder_min_free_gb",
        )
        return False
    log.info(
        "recorder.preflight_ok",
        free_gb=round(free / 2**30, 2),
        floor_gb=settings.recorder_min_free_gb,
    )
    return True


# ── Stop-signal wiring (C4, 2026-06-10) ───────────────────────────────────────


def _install_stop_signals(stop_event: asyncio.Event) -> None:
    """Wire SIGINT / SIGTERM / SIGBREAK to ``stop_event``.

    One Ctrl-C (or Ctrl-Break / kill) -> ``stop_event`` -> every task
    winds down (the dashboard included, since C3) -> ``run()``'s finally
    flushes and closes every recording gzip with a valid trailer.

    SIGBREAK is Windows-only and is also what a supervisor can deliver
    programmatically (``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT)`` to
    a process group), so shutdown drills can exercise the exact same
    stop path as a console Ctrl-C.
    """
    loop = asyncio.get_running_loop()
    sigs: list[signal.Signals] = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        sigs.append(sigbreak)
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows: loop.add_signal_handler is not supported.
            # signal.signal works for SIGINT / SIGBREAK; SIGTERM is
            # best-effort.
            try:
                signal.signal(sig, lambda *_: stop_event.set())
            except (ValueError, OSError):
                pass


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def run(
    dry_run: bool = True,
    record_dir: Path | None = None,
    *,
    mode: str = "live",
    duration_s: float | None = None,
    replay_date: str | None = None,
) -> int:
    """Run the agent.  Returns a process exit code:

    0 = clean run / clean stop; 1 = recorder fatal I/O failure stopped
    the run; 2 = --record-feeds preflight refused to start (disk too
    full).  Pre-existing callers that ignore the return value are
    unaffected.
    """
    _configure_logging()

    # Round 9c Commit 2: optional raw-feed recorder.  When ``record_dir``
    # is set (driven by ``--record-feeds`` on the CLI) every feed task
    # is wired to record raw frames into a per-source / per-day / per-hour
    # gzipped JSONL stream for future replay-mode validation.  Off by
    # default — recording is opt-in.
    # Replay mode READS recordings; it never records.  Building a recorder
    # in replay would risk appending to the very files we read.
    recorder: FrameRecorder | None = None
    # Set when the recorder fails fatally (ENOSPC etc.); drives the
    # nonzero exit code.  The on_fatal callback also sets stop_event
    # (late-bound closure; the event exists before any feed task runs).
    recorder_fatal: list[str] = []

    def _on_recorder_fatal(reason: str) -> None:
        recorder_fatal.append(reason)
        stop_event.set()

    if record_dir is not None and mode != "replay":
        if not _record_feeds_preflight(record_dir):
            return 2
        recorder = FrameRecorder(record_dir, on_fatal=_on_recorder_fatal)
        # Persistent evidence channel: the 2026-06-10 fatal WARNING went
        # to stdout only and scrolled away; the file log survives.
        configure_recorder_file_log(Path(settings.recorder_file_log_path))
        log.info(
            "frame_recorder.enabled",
            base_dir=str(record_dir),
            file_log=settings.recorder_file_log_path,
        )

    # Build step 1 (Fork 3): construct the simulated-clock seam from the
    # mode.  Live delegates to wall-clock; replay is advanceable.  In replay
    # the clock is left UNANCHORED (no start): the ReplayReader (build step 5)
    # positions it from the FIRST recorded frame's "ts" and advances it off
    # the recorded stream.  Anchoring at wall-clock would be a bug -- the
    # recorded frames predate "now", so the reader's monotonic guard would
    # never move the clock backwards onto the recorded timeline, freezing it
    # at wall-time and making every recorded tick look hours-stale.
    if mode == "replay":
        clock = SimulatedClock("replay")
    else:
        clock = SimulatedClock("live")

    agent = Agent(dry_run=dry_run, clock=clock)
    stop_event = asyncio.Event()

    _install_stop_signals(stop_event)

    log.info(
        "agent.starting",
        dry_run=dry_run,
        mode=mode,
        run_id=agent.run_id,
        duration_s=duration_s,
        min_edge=settings.min_edge,
        max_position_usd=settings.max_position_usd,
    )

    # Build step 5: replay swaps the live feed tasks (and the live scan /
    # settlement-poller / dashboard tasks) for the deterministic ReplayReader,
    # which ingests recorded frames, scans at the live 5 s sim-clock cadence,
    # and jump-to-expiry settles -- reaching NO network.  It runs to completion
    # and returns; the live TaskGroup below is never entered in replay mode.
    if mode == "replay":
        try:
            await _run_replay(agent, record_dir, replay_date)
        finally:
            await agent.order_mgr.aclose()
            summary = agent.paper_positions.performance_summary()
            log.info("agent.shutdown", **summary)
        return 0

    try:
        async with asyncio.TaskGroup() as tg:
            # Feed tasks are registered through _supervised_task so the
            # RecorderWatchdog (below, --record-feeds only) can restart a
            # dead one.  Without the watchdog the hooks are simply unused.
            restarters: dict[str, Callable[[], bool]] = {
                "deribit": _supervised_task(
                    tg, "deribit-feed",
                    lambda: _deribit_task(agent, stop_event, recorder=recorder),
                ),
                "kalshi": _supervised_task(
                    tg, "kalshi-feed",
                    lambda: _kalshi_task(agent, stop_event, recorder=recorder),
                ),
                "polymarket": _supervised_task(
                    tg, "polymarket-feed",
                    lambda: _polymarket_task(agent, stop_event, recorder=recorder),
                ),
            }
            # Capture-only auxiliary streams (fast spot / Chainlink round /
            # PM 5-min odds) for offline latency analysis -- ONLY under
            # --record-feeds (recorder set), and they touch no trading path.
            if recorder is not None:
                restarters.update(
                    _start_aux_capture_tasks(tg, stop_event, recorder)
                )
                watchdog = RecorderWatchdog(
                    recorder,
                    record_dir,
                    streams=_CAPTURE_STREAMS,
                    silence_threshold_s=settings.recorder_watchdog_silence_s,
                    check_interval_s=settings.recorder_watchdog_interval_s,
                    disk_soft_free_bytes=int(
                        settings.recorder_disk_soft_free_gb * 2**30
                    ),
                    restarters=restarters,
                    max_restarts_per_stream=(
                        settings.recorder_watchdog_max_restarts
                    ),
                )
                tg.create_task(
                    watchdog.run(stop_event), name="recorder-watchdog",
                )
            tg.create_task(_scan_task(agent, stop_event), name="scan")
            tg.create_task(_order_refresh_task(agent, stop_event), name="order-refresh")
            tg.create_task(agent.settlement_monitor.run(stop_event), name="settlement")
            tg.create_task(
                _paper_settlement_task(agent, stop_event), name="paper-settlement",
            )
            tg.create_task(_dashboard_task(agent, stop_event), name="dashboard")
            if duration_s is not None:
                tg.create_task(
                    _duration_task(stop_event, duration_s), name="duration-bound",
                )
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
    if recorder_fatal:
        log.critical("run.recorder_fatal_exit", reasons=recorder_fatal)
        return 1
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser.

    Factored out of :func:`main` so the ``--mode`` / ``--duration`` wiring
    is unit-testable without launching the agent.
    """
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
    # Build step 1 (plan section 3.1): mode switch + bounded-run flag.
    parser.add_argument(
        "--mode", choices=("live", "replay"), default="live",
        help=(
            "live (default): drive the agent off live feeds and wall-clock. "
            "replay: drive the simulated clock off recorded frames (the "
            "replay reader is a separate follow-up; this flag wires the "
            "clock seam only)."
        ),
    )
    parser.add_argument(
        "--duration", type=float, default=None, metavar="N",
        help=(
            "Bound the run to N seconds (live: wall-clock).  Omit for an "
            "unbounded run stopped by SIGINT/SIGTERM."
        ),
    )
    # Build step 5: which recorded capture day to replay (under --record-dir).
    # Default: the latest date present.  Ignored in live mode.
    parser.add_argument(
        "--replay-date", type=str, default=None, metavar="YYYY-MM-DD",
        help=(
            "Capture day to replay (--mode replay).  Default: the latest "
            "date present under --record-dir."
        ),
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    # Replay reads from --record-dir; recording writes to it.  Either intent
    # means run() should know the directory.
    if args.mode == "replay" or args.record_feeds:
        record_dir = args.record_dir
    else:
        record_dir = None
    exit_code = asyncio.run(
        run(
            dry_run=True,
            record_dir=record_dir,
            mode=args.mode,
            duration_s=args.duration,
            replay_date=args.replay_date,
        )
    )
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
