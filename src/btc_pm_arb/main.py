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

import asyncio
import logging
import os
import secrets
import signal
from datetime import datetime, timezone
from typing import AsyncIterator

import structlog
import uvicorn

from btc_pm_arb.config import settings
from btc_pm_arb.execution.orders import OrderManager
from btc_pm_arb.execution.positions import PositionTracker
from btc_pm_arb.execution.risk import RiskConfig, RiskManager
from btc_pm_arb.execution.settlement import SettlementMonitor
from btc_pm_arb.feeds.deribit import DeribitFeed
from btc_pm_arb.feeds.discovery import MarketDiscovery, run_discovery_loop
from btc_pm_arb.feeds.health import FeedHealthTracker
from btc_pm_arb.models import ArbitrageSignal, DataSource, OptionTick
from btc_pm_arb.pricing.cache import ProbabilityCache
from btc_pm_arb.pricing.digital_pricer import DigitalPricer
from btc_pm_arb.pricing.realized_vol import RealizedVolTracker
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.server.app import create_app
from btc_pm_arb.server.state import SharedState
from btc_pm_arb.signals.confidence import ConfidenceScorer
from btc_pm_arb.signals.edge import EdgeCalculator
from btc_pm_arb.signals.filters import FilterConfig, SignalFilter
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
        self.order_mgr = OrderManager(dry_run=dry_run)
        self.settlement_monitor = SettlementMonitor(self.tracker)

        live_token = secrets.token_urlsafe(16)
        self.shared_state = SharedState(dry_run=dry_run, live_mode_token=live_token)

        self._pending_ticks: list[OptionTick] = []

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

        async with self.shared_state.write() as s:
            s.feeds = feeds
            s.vol_surface = vol_surface
            s.volatility_regime = volatility_regime
            s.positions = positions
            s.positions_summary = perf
            s.settlements = settlements
            s.performance = perf

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


# ── Tasks ──────────────────────────────────────────────────────────────────────

async def _deribit_task(agent: Agent, stop_event: asyncio.Event) -> None:
    log.info("deribit_task.starting", url=settings.deribit_url)
    while not stop_event.is_set():
        try:
            async with DeribitFeed(url=settings.deribit_url) as feed:
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

async def run(dry_run: bool = True) -> None:
    _configure_logging()

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
            tg.create_task(_deribit_task(agent, stop_event), name="deribit-feed")
            tg.create_task(_scan_task(agent, stop_event), name="scan")
            tg.create_task(_order_refresh_task(agent, stop_event), name="order-refresh")
            tg.create_task(agent.settlement_monitor.run(stop_event), name="settlement")
            tg.create_task(_dashboard_task(agent, stop_event), name="dashboard")
    except* KeyboardInterrupt:
        stop_event.set()
    except* Exception as eg:
        for exc in eg.exceptions:
            log.error("agent.task_error", error=str(exc))
        stop_event.set()
    finally:
        await agent.order_mgr.aclose()
        summary = agent.tracker.performance_summary()
        log.info("agent.shutdown", **summary)


def main() -> None:
    asyncio.run(run(dry_run=True))


if __name__ == "__main__":
    main()
