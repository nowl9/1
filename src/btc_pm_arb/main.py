"""Entry point — orchestrates all layers.

Layer 1 (data ingestion) is implemented.  Layers 2-4 are stubs for now.
Run with:
    python -m btc_pm_arb.main
or after installing:
    btc-pm-arb
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from btc_pm_arb.config import settings
from btc_pm_arb.feeds.deribit import DeribitFeed

log = structlog.get_logger(__name__)


def _configure_logging() -> None:
    import logging
    import structlog

    log_level = getattr(logging, settings.log_level, logging.INFO)

    if settings.log_format == "json":
        processors = [
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


async def run() -> None:
    _configure_logging()
    log.info("starting", deribit_url=settings.deribit_url)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async with DeribitFeed(url=settings.deribit_url) as feed:
        log.info("deribit_feed_started")
        tick_count = 0
        async for tick in feed.ticks():
            if stop_event.is_set():
                break
            tick_count += 1
            if tick_count % 500 == 0:
                log.info(
                    "tick_sample",
                    instrument=tick.instrument_name,
                    mark_price=tick.mark_price,
                    underlying=tick.underlying_price,
                    ticks_received=tick_count,
                )

    log.info("shutdown_complete", ticks_received=tick_count)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
