"""Kalshi REST market-data feed for BTC binary contracts.

Architecture
------------
* REST polling, not WebSocket.  Symmetric with the planned Polymarket
  feed (Issue 5) and avoids Kalshi's more involved WS auth handshake.
* Two cadences run concurrently inside one connection cycle:
    - Discovery loop, every ``_DISCOVERY_INTERVAL`` (60 s):
      ``GET /trade-api/v2/markets?series_ticker=KXBTC&status=open``
      refreshes the set of active BTC contract tickers and their
      metadata (title, subtitle, close_time).
    - Poll loop, every ``_POLL_INTERVAL`` (5 s): for each tracked
      ticker, ``GET /trade-api/v2/markets/{ticker}/orderbook`` pulls
      the current best bid/ask, normalises into a
      :class:`PredictionMarketTick` and yields it via the public
      :meth:`ticks` async generator.
* Liveness reporting is via an optional ``on_alive`` callback fired on
  every successful HTTP response (discovery and orderbook polls
  alike).  This is how the dashboard's ``feed_health`` shows alive even
  when the demo BTC market universe is empty (zero contracts → zero
  ticks; without the callback, the feed would look dead).
* Auth re-uses the shared RSA-PSS helpers in
  :mod:`btc_pm_arb.feeds._kalshi_auth`; same code path as
  ``execution.orders.KalshiExecutor``.
* Failure / reconnect: same exponential-backoff pattern as
  ``DeribitFeed._run_forever``, with a slightly higher base delay
  (5 s) since REST endpoints don't benefit from tight reconnect cycles.

Kalshi orderbook semantics
--------------------------
The orderbook response contains *bid* levels for each side
(``yes`` and ``no``); asks are derived from the opposite side's bid::

    yes_ask_cents = 100 - max(no_bid_levels)
    no_ask_cents  = 100 - max(yes_bid_levels)

This is Kalshi's complementary-pair pricing — buying YES is identical
to selling NO at (1 - price).  See the orderbook → tick conversion in
:func:`_build_tick`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import structlog

from btc_pm_arb.feeds._kalshi_auth import load_key, signed_headers
from btc_pm_arb.feeds.normalizer import normalize_kalshi_tick
from btc_pm_arb.models import PredictionMarketTick

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# How often to refresh the list of tracked BTC contracts (seconds).
_DISCOVERY_INTERVAL: float = 60.0
# How often to poll each tracked contract's orderbook (seconds).
_POLL_INTERVAL: float = 5.0
# HTTP request timeout (seconds).
_HTTP_TIMEOUT: float = 10.0

# Kalshi series ticker prefix that scopes us to BTC binary contracts.  The
# `series_ticker` filter on /markets accepts an exact match; KXBTC is the
# canonical Kalshi BTC index series.  If a future round needs additional
# series (e.g. KXBTCD for daily), extend this set and the discovery filter.
_BTC_SERIES_TICKER: str = "KXBTC"

# Reconnection backoff parameters.
_RECONNECT_BASE: float = 5.0
_RECONNECT_MAX: float = 60.0
_RECONNECT_FACTOR: float = 2.0


# ── Feed ──────────────────────────────────────────────────────────────────────

class KalshiFeed:
    """Async Kalshi BTC-contract market-data feed.

    Usage::

        async def on_alive() -> None:
            agent.feed_health.record_tick(DataSource.KALSHI)

        feed = KalshiFeed(
            base_url=settings.kalshi_base_url,
            key_path=settings.kalshi_private_key_path,
            key_id=settings.kalshi_api_key_id,
            on_alive=on_alive,
        )
        async with feed:
            async for tick in feed.ticks():
                agent.ingest_pm_tick(tick)
    """

    def __init__(
        self,
        *,
        base_url: str,
        key_path: str,
        key_id: str,
        on_alive: Callable[[], None] | None = None,
        queue_maxsize: int = 10_000,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key_path = key_path
        self._key_id = key_id
        self._on_alive = on_alive
        self._queue: asyncio.Queue[PredictionMarketTick] = asyncio.Queue(maxsize=queue_maxsize)
        self._running = False
        self._private_key: Any | None = None
        self._client: httpx.AsyncClient | None = None
        # Ticker → market metadata dict (title, subtitle, close_time, …),
        # populated by _discover_markets and consumed by _poll_loop.
        self._tracked: dict[str, dict] = {}

    # ── Public interface ──────────────────────────────────────────────────────

    async def __aenter__(self) -> "KalshiFeed":
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="kalshi-feed")
        return self

    async def __aexit__(self, *_: object) -> None:
        self._running = False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def ticks(self) -> AsyncIterator[PredictionMarketTick]:
        """Yield PredictionMarketTick objects as they arrive from Kalshi."""
        while self._running:
            try:
                tick = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield tick
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue

    # ── Internal: connection lifecycle ────────────────────────────────────────

    async def _run_forever(self) -> None:
        """Reconnect loop with exponential backoff."""
        backoff = _RECONNECT_BASE
        while self._running:
            try:
                await self._connect_and_poll()
                # Clean exit (only on shutdown)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "kalshi_disconnected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX)
            else:
                backoff = _RECONNECT_BASE  # reset on clean reconnect

    async def _connect_and_poll(self) -> None:
        """Initialise auth + HTTP client, run discovery and poll concurrently."""
        logger.info("kalshi_connecting", base_url=self._base_url)
        self._private_key = load_key(self._key_path)
        if self._private_key is None:
            # load_key already logged the failure at warning level.  Raise
            # so the reconnect loop catches and applies backoff — repeated
            # backoff failures are the right user-visible signal for a
            # misconfigured KALSHI_PRIVATE_KEY_PATH.
            raise RuntimeError(f"Could not load Kalshi key at {self._key_path}")

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_HTTP_TIMEOUT,
        )
        try:
            # Initial discovery confirms connectivity (bad URL, bad key,
            # network error all surface here before the periodic loops
            # start).  Fires the on_alive callback on success.
            await self._discover_markets()
            logger.info(
                "kalshi_connected",
                base_url=self._base_url,
                tracked=len(self._tracked),
            )
            await asyncio.gather(
                self._discovery_loop(),
                self._poll_loop(),
            )
        finally:
            await self._client.aclose()
            self._client = None

    # ── Internal: HTTP helper ─────────────────────────────────────────────────

    async def _http_get(self, path: str) -> dict:
        """Authenticated GET, returning the JSON-decoded body.

        Fires ``on_alive`` on every 2xx response — this is what makes the
        feed register liveness even when the BTC market universe is empty.
        """
        assert self._client is not None
        headers = signed_headers("GET", path, self._private_key, self._key_id)
        resp = await self._client.get(path, headers=headers)
        resp.raise_for_status()
        if self._on_alive is not None:
            self._on_alive()
        return resp.json()

    # ── Internal: discovery ───────────────────────────────────────────────────

    async def _discover_markets(self) -> None:
        """Refresh the tracked BTC contract list.

        Replaces self._tracked atomically; tickers no longer present in
        the response (e.g. closed) are dropped.
        """
        path = (
            f"/trade-api/v2/markets"
            f"?series_ticker={_BTC_SERIES_TICKER}"
            f"&status=open"
            f"&limit=200"
        )
        body = await self._http_get(path)
        markets = body.get("markets", []) or []
        new_tracked: dict[str, dict] = {}
        for m in markets:
            ticker = m.get("ticker")
            if ticker:
                new_tracked[ticker] = m
        self._tracked = new_tracked
        logger.info(
            "kalshi_discovery_completed",
            tracked=len(self._tracked),
        )

    async def _discovery_loop(self) -> None:
        """Background: refresh discovery every ``_DISCOVERY_INTERVAL``."""
        while self._running:
            await asyncio.sleep(_DISCOVERY_INTERVAL)
            if not self._running:
                break
            try:
                await self._discover_markets()
            except Exception as exc:
                # Discovery failure is non-fatal — keep using the previously
                # tracked set until the next interval.  Persistent failures
                # will eventually fail the next /orderbook calls and trigger
                # the outer reconnect loop.
                logger.warning(
                    "kalshi_discovery_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    # ── Internal: orderbook polling ───────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Background: poll each tracked ticker's orderbook every ``_POLL_INTERVAL``."""
        while self._running:
            tickers = list(self._tracked.keys())
            ticks_emitted = 0
            for ticker in tickers:
                if not self._running:
                    break
                meta = self._tracked.get(ticker)
                if meta is None:
                    continue
                try:
                    book_body = await self._http_get(
                        f"/trade-api/v2/markets/{ticker}/orderbook"
                    )
                except Exception as exc:
                    # One ticker's failure shouldn't break the loop.  Log
                    # and move on.  If the failure is systemic, the next
                    # _http_get will eventually raise something the outer
                    # reconnect catches.
                    logger.warning(
                        "kalshi_orderbook_error",
                        ticker=ticker,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    continue
                book = book_body.get("orderbook") or {}
                tick = _build_tick(ticker, meta, book)
                if tick is None:
                    continue
                try:
                    self._queue.put_nowait(tick)
                except asyncio.QueueFull:
                    # Drop oldest, retry — same pattern as DeribitFeed.
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._queue.put_nowait(tick)
                    except asyncio.QueueFull:
                        logger.warning("kalshi_queue_full_drop", ticker=ticker)
                        continue
                ticks_emitted += 1
            logger.debug(
                "kalshi_poll_completed",
                tracked=len(self._tracked),
                emitted=ticks_emitted,
            )
            await asyncio.sleep(_POLL_INTERVAL)


# ── Tick builder ──────────────────────────────────────────────────────────────

def _build_tick(ticker: str, meta: dict, book: dict) -> PredictionMarketTick | None:
    """Combine cached market metadata with a fresh orderbook into a tick.

    Kalshi orderbook semantics: each side has *bid* levels only.  Asks are
    derived from the opposite side (yes_ask = 100 - best_no_bid).  Returns
    None if the orderbook has no bids on either side (no quotes available).
    """
    yes_levels = book.get("yes") or []
    no_levels = book.get("no") or []

    # Each level is [price_cents, quantity].  We want the highest bid on
    # each side (best price the market is offering to pay).
    def _best_bid_cents(levels: list) -> int | None:
        if not levels:
            return None
        try:
            return max(int(p) for p, _ in levels)
        except (TypeError, ValueError):
            return None

    yes_best_bid = _best_bid_cents(yes_levels)
    no_best_bid = _best_bid_cents(no_levels)

    # Complementary-pair derivation: ask comes from the opposite side's bid.
    yes_best_ask = (100 - no_best_bid) if no_best_bid is not None else None
    no_best_ask = (100 - yes_best_bid) if yes_best_bid is not None else None

    if yes_best_bid is None and yes_best_ask is None:
        # No quotes at all — nothing to emit.
        return None

    raw = {
        "ticker": ticker,
        "title": meta.get("title", "") or "",
        "subtitle": meta.get("subtitle", "") or "",
        "yes_bid": yes_best_bid,
        "yes_ask": yes_best_ask,
        "no_bid": no_best_bid,
        "no_ask": no_best_ask,
        "close_time": meta.get("close_time"),
    }
    return normalize_kalshi_tick(raw)
