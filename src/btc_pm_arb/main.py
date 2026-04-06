"""Entry point — wires all four layers into a running arbitrage agent.

Pipeline::

    DeribitFeed (ticks) ──► VolSurface + DigitalPricer + ProbabilityCache
                                    │
                                    ▼
              PredictionMarket feeds ──► ContractMatcher
                                              │
                                              ▼
                                       EdgeCalculator
                                              │
                                              ▼
                                      SignalFilter (criteria chain)
                                              │
                                              ▼
                                     ConfidenceScorer
                                              │
                                              ▼
                                     RiskManager (pre-trade)
                                              │
                                              ▼
                                      OrderManager ──► positions / settlement

Modes
-----
* DRY RUN (default): All signals and theoretical orders are logged; no orders
  are submitted to any platform.
* LIVE: Set ``dry_run=False`` in Settings (or pass ``--live`` once that flag
  is wired up).  Requires valid API keys in the environment.

Concurrency
-----------
* Python 3.11+ ``asyncio.TaskGroup`` keeps all tasks under one supervisor.
* SIGINT / SIGTERM: sets a shared ``stop_event``; all tasks check it and exit.
* Each feed runs in its own task; the scan loop is a separate task; the
  settlement monitor is a fourth task.

Structured logging
------------------
* ``structlog`` configured once in ``_configure_logging()``.
* All events carry a ``component`` key for easy filtering.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import AsyncIterator

import structlog

from btc_pm_arb.config import settings
from btc_pm_arb.execution.orders import OrderManager
from btc_pm_arb.execution.positions import PositionTracker
from btc_pm_arb.execution.risk import RiskConfig, RiskManager
from btc_pm_arb.execution.settlement import SettlementMonitor
from btc_pm_arb.feeds.deribit import DeribitFeed
from btc_pm_arb.models import ArbitrageSignal, OptionTick
from btc_pm_arb.pricing.cache import ProbabilityCache
from btc_pm_arb.pricing.digital_pricer import DigitalPricer
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.signals.confidence import ConfidenceScorer
from btc_pm_arb.signals.edge import EdgeCalculator
from btc_pm_arb.signals.filters import FilterConfig, SignalFilter
from btc_pm_arb.signals.matcher import ContractMatcher

log: structlog.BoundLogger = structlog.get_logger("main")

# ── Default scan interval ─────────────────────────────────────────────────────
_SCAN_INTERVAL_SECS: float = 5.0
_ORDER_REFRESH_SECS: float = 30.0
_BASE_SIZE_USD: float = 200.0    # per-trade notional before confidence scaling


# ── Logging setup ─────────────────────────────────────────────────────────────

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


# ── Agent components ──────────────────────────────────────────────────────────

class Agent:
    """Container for all stateful pipeline components.

    Constructed once and passed to each async task.  All tasks share the same
    vol surface, cache, tracker, etc. — no locks needed because all access is
    from the same event loop.
    """

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self.surface = VolSurface()
        self.cache = ProbabilityCache()
        self.pricer = DigitalPricer()
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

        # Buffer of recent ticks, flushed to surface on each scan
        self._pending_ticks: list[OptionTick] = []

    def ingest_tick(self, tick: OptionTick) -> None:
        self._pending_ticks.append(tick)

    def flush_ticks(self) -> set:
        """Push pending ticks to vol surface; return set of dirty expiries."""
        if not self._pending_ticks:
            return set()
        dirty = self.surface.update(self._pending_ticks)
        self._pending_ticks.clear()
        return dirty

    def update_cache_from_surface(self, dirty_expiries: set) -> None:
        """Re-price digitals for all dirty expiries and push to cache."""
        from btc_pm_arb.models import DataSource
        for expiry in dirty_expiries:
            smile = self.surface.get_smile(expiry)
            if smile is None or smile.forward is None:
                continue
            # Collect strikes from all ticks for this expiry that live in the surface
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


# ── Pipeline tasks ────────────────────────────────────────────────────────────

async def _deribit_task(agent: Agent, stop_event: asyncio.Event) -> None:
    """Feed Deribit ticks into the agent's surface buffer."""
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


async def _scan_task(
    agent: Agent,
    stop_event: asyncio.Event,
    pm_tick_source: AsyncIterator | None = None,
) -> None:
    """Periodically flush ticks, update cache, scan for signals, and place orders."""
    log.info("scan_task.starting", interval_secs=_SCAN_INTERVAL_SECS)
    while not stop_event.is_set():
        try:
            await asyncio.sleep(_SCAN_INTERVAL_SECS)
            if stop_event.is_set():
                break

            # ── pricing engine step ────────────────────────────────────────
            dirty = agent.flush_ticks()
            if dirty:
                agent.update_cache_from_surface(dirty)
                log.debug("scan.surface_updated", dirty_expiries=len(dirty))

            # ── signal generation step ─────────────────────────────────────
            # In production, pm_ticks come from live PM feeds.
            # In dry-run / test, they are injected via pm_tick_source.
            pm_ticks = []
            if pm_tick_source is not None:
                try:
                    while True:
                        pm_ticks.append(pm_tick_source.__anext__())  # type: ignore[attr-defined]
                except StopAsyncIteration:
                    pass

            if not pm_ticks:
                continue

            matches = agent.matcher.batch_match(pm_ticks, agent.cache)
            edges = [agent.edge_calc.compute(m, surface=agent.surface) for m in matches]
            signals = agent.signal_filter.filter(edges, surface=agent.surface)

            # Score confidence and run through risk + order manager
            for sig in signals:
                confidence = agent.confidence_scorer.score(
                    next(e for e in edges if e.match.pm_tick.contract_id == sig.pm_quote.contract_id),
                    surface=agent.surface,
                )
                # Attach confidence back to signal (ArbitrageSignal is a Pydantic model)
                sig = sig.model_copy(update={"confidence": confidence})

                proposed_size = agent.risk.size_for_signal(sig, _BASE_SIZE_USD, agent.tracker)
                decision = agent.risk.check(sig, proposed_size, agent.tracker)
                if not decision:
                    continue

                order = await agent.order_mgr.place(sig, size_usd=proposed_size)
                if order is not None and order.state.value in {"placed", "filled"}:
                    # Immediately record fill for dry-run simulated fills
                    await agent.order_mgr.refresh_all()
                    for o in agent.order_mgr.filled_orders():
                        pos = agent.tracker.record_fill(o)
                        if pos is not None:
                            agent.settlement_monitor.track(
                                contract_id=o.contract_id,
                                platform=o.platform,
                                expiry=sig.pm_quote.expiry,
                                theoretical_edge=sig.adjusted_edge,
                                side=o.side,
                                entry_price=o.average_fill_price or o.limit_price,
                                size_usd=o.filled_size,
                            )

        except Exception as exc:
            log.error("scan_task.error", error=str(exc))

    log.info("scan_task.stopped")


async def _order_refresh_task(agent: Agent, stop_event: asyncio.Event) -> None:
    """Periodically poll open order status."""
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


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def run(dry_run: bool = True) -> None:
    _configure_logging()

    agent = Agent(dry_run=dry_run)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

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
    except* KeyboardInterrupt:
        stop_event.set()
    except* Exception as eg:
        for exc in eg.exceptions:
            log.error("agent.task_error", error=str(exc))
        stop_event.set()
    finally:
        await agent.order_mgr.aclose()
        summary = agent.tracker.performance_summary()
        settlement_summary = agent.settlement_monitor.performance_summary()
        log.info("agent.shutdown", **summary, **{f"settlement_{k}": v for k, v in settlement_summary.items()})


def main() -> None:
    asyncio.run(run(dry_run=True))


if __name__ == "__main__":
    main()
