"""Polymarket public market-data feed for BTC binary contracts.

Architecture
------------
* PUBLIC DATA ONLY.  The agent runs without any Polymarket API
  credentials (US-restricted; trading not supported — see
  ``OrderManager``'s short-circuit for any signal targeting
  Polymarket).  Two unauthenticated REST APIs are polled:
    - Gamma (``polymarket_gamma_url``): market discovery via
      ``GET /markets?active=true&closed=false&limit=500&order=volume&ascending=false``
      returning market metadata (question, outcomes, clobTokenIds,
      endDate).
    - CLOB (``polymarket_clob_url``): per-contract order books via
      ``GET /book?token_id={yes_token_id}`` returning bids/asks as
      string-encoded floats in [0, 1].
* Two cadences run concurrently inside one connection cycle:
    - Discovery loop, every ``_DISCOVERY_INTERVAL`` (60 s): refreshes
      the tracked set of BTC binary threshold markets via the gamma API.
    - Poll loop, every ``_POLL_INTERVAL`` (5 s): for each tracked YES
      token id, fetches the latest CLOB book, builds a
      :class:`PredictionMarketTick` and yields it via :meth:`ticks`.
* URL-construction convention (lesson from Round 7a): both
  ``polymarket_gamma_url`` and ``polymarket_clob_url`` operate at host
  root with no path suffix.  Request paths in this module are therefore
  written as ``/markets``, ``/book``, etc.  Never re-prepend a host or
  ``/gamma-api/`` / ``/clob/`` prefix — the doubled-prefix bug from
  KalshiFeed must not recur here.  Regression covered by
  ``tests/test_feeds/test_polymarket.py::test_request_paths_are_relative_to_base_urls``.
* Liveness reporting is via an optional ``on_alive`` callback fired on
  every successful HTTP response (discovery and book polls alike) — the
  same pattern as KalshiFeed.  This keeps the dashboard's
  ``feed_health`` row showing OK even when the BTC market universe is
  briefly empty.
* Failure / reconnect: same exponential-backoff pattern as KalshiFeed
  (5 s base, 60 s max, 2× factor).  Per-contract book-fetch errors are
  logged at warning and the loop moves on; only systemic failure
  surfaces to the outer reconnect loop.

Polymarket binary-market shape
------------------------------
A BTC threshold market on Polymarket is a binary YES/NO market with::

    {
      "active": true,
      "closed": false,
      "question": "Will Bitcoin reach $100,000 by Apr 30?",
      "outcomes": ["Yes", "No"],
      "clobTokenIds": ["<yes_token_id>", "<no_token_id>"],
      "endDate": "2026-04-30T23:59:00Z",
      ...
    }

The YES token id (the asset_id we pass to ``/book``) is resolved by
*name* — index of the "Yes" entry in ``outcomes`` — to be defensive
against future ordering changes.  NO-side prices are derived from the
YES book by complementary pricing inside
:func:`btc_pm_arb.feeds.normalizer.normalize_polymarket_tick`.

Tick construction routes through ``normalize_polymarket_tick`` (mirror
of Kalshi's ``_build_tick → normalize_kalshi_tick`` flow).  This keeps
expiry parsing, strike extraction, complementary no-side derivation,
and timestamp handling centralised in one normalizer with a single set
of unit tests; the feed module only does I/O and aggregation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from btc_pm_arb.feeds.normalizer import (
    _extract_strike_from_question,
    normalize_polymarket_tick,
)
from btc_pm_arb.feeds.recorder import FrameRecorder
from btc_pm_arb.models import DataSource, PredictionMarketTick

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# How often to refresh the tracked BTC contract set (seconds).
_DISCOVERY_INTERVAL: float = 60.0
# How often to poll each tracked contract's order book (seconds).
_POLL_INTERVAL: float = 5.0
# HTTP request timeout (seconds).
_HTTP_TIMEOUT: float = 10.0

# Reconnection backoff parameters — match KalshiFeed.
_RECONNECT_BASE: float = 5.0
_RECONNECT_MAX: float = 60.0
_RECONNECT_FACTOR: float = 2.0

# Discovery query: server-side filter narrows to active+open markets;
# client-side ``_is_btc_binary_threshold`` does the BTC + binary +
# threshold gating.  We deliberately don't filter by tag_id here —
# Polymarket tag IDs have changed historically and content-based
# filtering is more durable.
_DISCOVERY_LIMIT: int = 500


# ── Feed ──────────────────────────────────────────────────────────────────────

class PolymarketFeed:
    """Async Polymarket BTC binary-contract market-data feed.

    Public data only — no API credentials required.  Trading is handled
    elsewhere (and currently short-circuited; see module docstring).

    Usage::

        async def on_alive() -> None:
            agent.feed_health.record_tick(DataSource.POLYMARKET)

        feed = PolymarketFeed(
            gamma_url=settings.polymarket_gamma_url,
            clob_url=settings.polymarket_clob_url,
            on_alive=on_alive,
        )
        async with feed:
            async for tick in feed.ticks():
                agent.ingest_pm_tick(tick)
    """

    def __init__(
        self,
        *,
        gamma_url: str,
        clob_url: str,
        on_alive: Callable[[], None] | None = None,
        queue_maxsize: int = 10_000,
        recorder: FrameRecorder | None = None,
    ) -> None:
        self._gamma_url = gamma_url.rstrip("/")
        self._clob_url = clob_url.rstrip("/")
        self._on_alive = on_alive
        self._queue: asyncio.Queue[PredictionMarketTick] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._running = False
        self._gamma_client: httpx.AsyncClient | None = None
        self._clob_client: httpx.AsyncClient | None = None
        # Round 9c Commit 2: optional raw-response recorder for replay.
        # None by default — opt-in via main.py's ``--record-feeds``.
        self._recorder = recorder
        # YES token_id → market metadata dict.  Populated by
        # _discover_markets and consumed by _poll_loop.  Keying on the
        # YES token id matches the contract_id we eventually emit on
        # the tick (see _build_tick), so downstream lookups round-trip
        # cleanly back to /book.
        self._tracked: dict[str, dict] = {}

    # ── Public interface ──────────────────────────────────────────────────────

    async def __aenter__(self) -> "PolymarketFeed":
        self._running = True
        self._task = asyncio.create_task(
            self._run_forever(), name="polymarket-feed"
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        self._running = False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def ticks(self) -> AsyncIterator[PredictionMarketTick]:
        """Yield PredictionMarketTick objects as they arrive from Polymarket."""
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
                    "polymarket_disconnected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX)
            else:
                backoff = _RECONNECT_BASE  # reset on clean reconnect

    async def _connect_and_poll(self) -> None:
        """Open both REST clients, run discovery, then start the polling loops."""
        logger.info(
            "polymarket_connecting",
            gamma_url=self._gamma_url,
            clob_url=self._clob_url,
        )
        self._gamma_client = httpx.AsyncClient(
            base_url=self._gamma_url,
            timeout=_HTTP_TIMEOUT,
        )
        self._clob_client = httpx.AsyncClient(
            base_url=self._clob_url,
            timeout=_HTTP_TIMEOUT,
        )
        try:
            # Initial discovery confirms connectivity (bad URL, network
            # error, malformed response all surface here before the
            # periodic loops start).  Fires on_alive on success.
            await self._discover_markets()
            logger.info(
                "polymarket_connected",
                gamma_url=self._gamma_url,
                tracked=len(self._tracked),
            )
            await asyncio.gather(
                self._discovery_loop(),
                self._poll_loop(),
            )
        finally:
            await self._gamma_client.aclose()
            await self._clob_client.aclose()
            self._gamma_client = None
            self._clob_client = None

    # ── Internal: HTTP helper ─────────────────────────────────────────────────

    async def _http_get(
        self, client: httpx.AsyncClient, path: str
    ) -> Any:
        """Unauthenticated GET, returning the JSON-decoded body.

        Fires ``on_alive`` on every 2xx response — see KalshiFeed for the
        same liveness pattern.
        """
        resp = await client.get(path)
        resp.raise_for_status()
        # Round 9c Commit 2: record the raw response body (decompressed
        # by httpx already) before json parsing.  Same shape as the
        # KalshiFeed hook — 4xx/5xx already raised, so the recorder
        # only sees 2xx bodies; failures land in agent.log via
        # structlog.  Recording happens before on_alive so a recorder
        # failure can't suppress the liveness signal (recorder.record()
        # never raises — it self-disables).
        if self._recorder is not None:
            self._recorder.record(
                DataSource.POLYMARKET,
                resp.content,
                datetime.now(timezone.utc),
                endpoint=path,
            )
        if self._on_alive is not None:
            self._on_alive()
        return resp.json()

    # ── Internal: discovery ───────────────────────────────────────────────────

    async def _discover_markets(self) -> None:
        """Refresh the tracked BTC binary-threshold contract set.

        Replaces self._tracked atomically; YES token ids that drop out of
        the response (closed, expired, or no longer matching the BTC
        binary-threshold filter) are removed.
        """
        # Path is relative to settings.polymarket_gamma_url, which has no
        # path suffix by convention.  Do NOT prepend '/gamma-api/' or
        # similar — that's the same shape of bug as the doubled-prefix
        # 404 caught in Round 7a (regression-covered in
        # tests/test_feeds/test_polymarket.py).
        path = (
            "/markets"
            "?active=true"
            "&closed=false"
            f"&limit={_DISCOVERY_LIMIT}"
            "&order=volume"
            "&ascending=false"
        )
        body = await self._http_get(self._gamma_client, path)
        # Gamma /markets has historically returned either a bare list or
        # a {"data": [...]} envelope — accommodate both rather than assume.
        markets: list[dict]
        if isinstance(body, list):
            markets = body
        elif isinstance(body, dict):
            markets = body.get("data") or body.get("markets") or []
        else:
            markets = []

        new_tracked: dict[str, dict] = {}
        for market in markets:
            if not _is_btc_binary_threshold(market):
                continue
            yes_token_id = _resolve_yes_token(market)
            if yes_token_id is None:
                logger.debug(
                    "polymarket_market_rejected",
                    reason="missing_yes_token",
                    question=market.get("question"),
                )
                continue
            new_tracked[yes_token_id] = market
        self._tracked = new_tracked
        logger.info(
            "polymarket_discovery_completed",
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
                # eventually fail the next /book calls and trigger the
                # outer reconnect loop.
                logger.warning(
                    "polymarket_discovery_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    # ── Internal: order-book polling ──────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Background: poll each tracked YES token's CLOB book every ``_POLL_INTERVAL``."""
        while self._running:
            tracked_snapshot = list(self._tracked.items())
            ticks_emitted = 0
            for yes_token_id, market_meta in tracked_snapshot:
                if not self._running:
                    break
                try:
                    # Path relative to clob base_url; see _discover_markets.
                    book = await self._http_get(
                        self._clob_client,
                        f"/book?token_id={yes_token_id}",
                    )
                except Exception as exc:
                    # One contract's failure shouldn't break the loop.  Log
                    # and move on.  Systemic failure (all requests failing)
                    # eventually surfaces via the outer reconnect.
                    logger.warning(
                        "polymarket_orderbook_error",
                        token_id=yes_token_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    continue

                tick = _build_tick(yes_token_id, market_meta, book or {})
                if tick is None:
                    continue
                try:
                    self._queue.put_nowait(tick)
                except asyncio.QueueFull:
                    # Drop oldest, retry — same pattern as DeribitFeed /
                    # KalshiFeed.
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._queue.put_nowait(tick)
                    except asyncio.QueueFull:
                        logger.warning(
                            "polymarket_queue_full_drop",
                            token_id=yes_token_id,
                        )
                        continue
                ticks_emitted += 1
            logger.debug(
                "polymarket_poll_completed",
                tracked=len(self._tracked),
                emitted=ticks_emitted,
            )
            await asyncio.sleep(_POLL_INTERVAL)


# ── Filter / token resolution ─────────────────────────────────────────────────


def _coerce_to_list(value: object) -> list:
    """Accept a list or a JSON-encoded list string, return a list.

    Polymarket's gamma API returns ``outcomes`` and ``clobTokenIds`` as
    JSON-encoded strings (e.g. ``'["Yes","No"]'``) rather than native
    lists.  Returns an empty list on any decode failure or unexpected
    type, matching the fail-closed behavior of the binary-shape filter.

    Regression: without this coercion every market was rejected as
    ``reason=not_binary`` because ``isinstance("...", list)`` is False
    (``tracked=0`` runtime observation on commit 8260a11).
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _is_btc_binary_threshold(market: dict) -> bool:
    """True iff ``market`` is an active BTC binary YES/NO threshold market.

    Rejection reasons are logged at DEBUG under
    ``polymarket_market_rejected`` with a ``reason=`` field so dropped
    markets are diagnosable from the log without rerunning.  The filter
    intentionally fails closed: any malformed or under-specified market
    is rejected.
    """
    question_for_log = market.get("question")

    if not market.get("active"):
        logger.debug(
            "polymarket_market_rejected",
            reason="not_active",
            question=question_for_log,
        )
        return False
    if market.get("closed"):
        logger.debug(
            "polymarket_market_rejected",
            reason="closed",
            question=question_for_log,
        )
        return False

    outcomes = _coerce_to_list(market.get("outcomes"))
    if not (
        isinstance(outcomes, list)
        and len(outcomes) == 2
        and {"yes", "no"} <= {str(o).strip().lower() for o in outcomes}
    ):
        logger.debug(
            "polymarket_market_rejected",
            reason="not_binary",
            question=question_for_log,
        )
        return False

    token_ids = _coerce_to_list(market.get("clobTokenIds"))
    if not (isinstance(token_ids, list) and len(token_ids) == 2):
        logger.debug(
            "polymarket_market_rejected",
            reason="missing_token_ids",
            question=question_for_log,
        )
        return False

    question_lower = (market.get("question") or "").lower()
    if "bitcoin" not in question_lower and "btc" not in question_lower:
        logger.debug(
            "polymarket_market_rejected",
            reason="not_btc",
            question=question_for_log,
        )
        return False

    if _extract_strike_from_question(market.get("question") or "") is None:
        logger.debug(
            "polymarket_market_rejected",
            reason="no_strike",
            question=question_for_log,
        )
        return False

    return True


def _resolve_yes_token(market: dict) -> str | None:
    """Return the YES side's CLOB token id from a binary-market metadata dict.

    Resolves by *name* (lower-cased "yes" in ``outcomes``) rather than
    assuming index 0 — defensive against future ordering changes.  Returns
    ``None`` for any market whose outcomes/clobTokenIds shape doesn't match
    the expected binary layout.
    """
    outcomes = _coerce_to_list(market.get("outcomes"))
    token_ids = _coerce_to_list(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None
    for idx, name in enumerate(outcomes):
        if str(name).strip().lower() == "yes":
            return str(token_ids[idx])
    return None


# ── Tick builder ──────────────────────────────────────────────────────────────


def _build_tick(
    yes_token_id: str, market_meta: dict, book: dict
) -> PredictionMarketTick | None:
    """Combine cached gamma metadata with a fresh CLOB book into a tick.

    Mirrors the Kalshi pattern (``KalshiFeed._build_tick`` →
    ``normalize_kalshi_tick``): pre-aggregate the best bid / best ask
    defensively, then route through :func:`normalize_polymarket_tick`
    so expiry parsing, strike extraction, no-side complementary pricing
    and timestamp handling stay centralised in the normalizer.

    Defensive aggregation: although the CLOB ``/book`` response is
    documented as bids-descending / asks-ascending, we compute
    ``best_bid = max(prices)`` and ``best_ask = min(prices)`` ourselves
    rather than trust ``levels[0]``.  Matches Kalshi's defensive
    ``max()`` over price levels in ``KalshiFeed._build_tick``.

    Returns ``None`` if the book has no bids and no asks — same
    "nothing to emit" semantics as KalshiFeed.
    """
    raw_bids = book.get("bids") or []
    raw_asks = book.get("asks") or []

    def _aggregate(
        levels: list, picker: Callable[[list[float]], float]
    ) -> float | None:
        prices: list[float] = []
        for level in levels:
            try:
                prices.append(float(level.get("price")))
            except (TypeError, ValueError, AttributeError):
                continue
        if not prices:
            return None
        return picker(prices)

    best_bid = _aggregate(raw_bids, max)
    best_ask = _aggregate(raw_asks, min)

    if best_bid is None and best_ask is None:
        return None

    raw: dict = {
        # YES token id becomes the contract_id (via the normalizer's
        # condition_id-then-token_id fallback) so /book lookups round-trip
        # cleanly downstream.  We deliberately do NOT pass condition_id
        # here even when present, to keep contract_id == asset_id.
        "token_id": yes_token_id,
        "question": market_meta.get("question") or "",
        "outcomes": market_meta.get("outcomes") or ["Yes", "No"],
        "endDate": market_meta.get("endDate") or market_meta.get("end_date_iso"),
        "bids": [{"price": str(best_bid)}] if best_bid is not None else [],
        "asks": [{"price": str(best_ask)}] if best_ask is not None else [],
    }
    return normalize_polymarket_tick(raw)
