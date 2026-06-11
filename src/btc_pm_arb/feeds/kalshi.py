"""Kalshi REST market-data feed for BTC binary contracts.

Architecture
------------
* REST polling, not WebSocket.  Symmetric with the planned Polymarket
  feed (Issue 5) and avoids Kalshi's more involved WS auth handshake.
* Two cadences run concurrently inside one connection cycle:
    - Discovery loop, every ``_DISCOVERY_INTERVAL`` (60 s):
      ``GET {base_url}/markets?series_ticker=KXBTC&status=open``
      refreshes the set of active BTC contract tickers and their
      metadata (title, subtitle, close_time).
    - Poll loop, every ``_POLL_INTERVAL`` (5 s): for each tracked
      ticker, ``GET {base_url}/markets/{ticker}/orderbook`` pulls
      the current best bid/ask, normalises into a
      :class:`PredictionMarketTick` and yields it via the public
      :meth:`ticks` async generator.
* URL-construction convention: ``settings.kalshi_base_url`` already ends
  in ``/trade-api/v2`` (see ``config.Settings.kalshi_demo_url`` /
  ``kalshi_prod_url``).  Request paths in this module are therefore
  written as ``/markets``, ``/markets/{ticker}/orderbook``, etc., NOT
  ``/trade-api/v2/markets``.  Doubling the prefix produces a 404 (caught
  at runtime in round 6 verification).
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
The orderbook response wrapper key is ``orderbook_fp`` (was
``orderbook`` pre-March-2026; renamed as part of Kalshi's fixed-point
dollar-string migration).  The inner dict contains *bid* levels for
each side (``yes_dollars`` and ``no_dollars``, was ``yes`` / ``no``);
asks are derived from the opposite side's bid::

    yes_ask = 1.0 - max(no_bid_levels_as_floats)
    no_ask  = 1.0 - max(yes_bid_levels_as_floats)

Each level is a 2-tuple ``[price_dollars_str, qty_str]``.  The price
is a fixed-point dollar string in [0, 1] (e.g. ``"0.6200"`` = 62¢);
both tuple elements need ``float()`` casting.  Pre-migration these
were integer cents in [1, 99] — silently emitting zero ticks if the
old shape is assumed (caught via the Round 7c pre-task observation).

This is Kalshi's complementary-pair pricing — buying YES is identical
to selling NO at (1 - price).  See the orderbook → tick conversion in
:func:`_build_tick`.  Round-trip regression covered by
``tests/test_feeds/test_kalshi.py::TestBuildTickDollarRoundTrip``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from btc_pm_arb.feeds._kalshi_auth import load_key, signed_headers
from btc_pm_arb.feeds.normalizer import normalize_kalshi_tick
from btc_pm_arb.feeds.recorder import FrameRecorder
from btc_pm_arb.models import DataSource, PredictionMarketTick

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# How often to refresh the list of tracked BTC contracts (seconds).
_DISCOVERY_INTERVAL: float = 60.0
# How often to poll each tracked contract's orderbook (seconds).
_POLL_INTERVAL: float = 5.0
# HTTP request timeout (seconds).
_HTTP_TIMEOUT: float = 10.0

# Kalshi BTC series allow-list.  `series_ticker` on /markets is a
# server-side EXACT match, NOT a prefix — so a single GET can scope
# to only one series at a time.  Round 9d2 verify (live API probe,
# 2026-05) found that the canonical KXBTC family is split across many
# series, only some of which match this agent's mandate (threshold
# binary contracts with close_time in [1, 90] days):
#
#   Tier 1 (live, in-window contracts confirmed by 9d2 verify):
#     KXBTCMINMON  — monthly "min price" thresholds
#     KXBTCMAXMON  — monthly "max price" thresholds
#     KXBTCMAX150  — $150k max-price binaries
#
#   Tier 2 (series historically active but currently returning zero
#     open contracts — included so the discovery pipeline picks them
#     up automatically the next time Kalshi opens contracts on these
#     series; revisit after a 24-48h re-probe to decide if any are
#     actually deprecated):
#     KXBTCW, KXBTCMAXW, BTC, BTCD
#
#   EXCLUDED (do NOT re-add without verification — they break this
#     agent's contract shape or horizon):
#     KXBTC      — hourly "Bitcoin range" markets, not threshold-binary
#     KXBTCD     — hourly above/below intraday, outside the [1, 90]d window
#     KXBTC15M   — 15-minute intraday, outside the [1, 90]d window
#     KXBTCMAX100, KXBTCMAX125, KXBTCMAX200 — their currently-open
#       contracts close ~217d out, outside the [1, 90]d window; add
#       back ONLY when a probe shows in-window contracts.
#
# The pre-9d2 implementation pinned discovery to series_ticker=KXBTC
# alone, which is why the smoke fired zero signals: the canonical
# "KXBTC" exact match returns the intraday range markets we don't
# want, while the threshold-binary series that match the mandate sit
# under different series tickers.  Discovery now fans out one GET
# per allow-listed ticker and union-merges the results into _tracked.
_BTC_SERIES_TICKERS_TIER1: tuple[str, ...] = (
    "KXBTCMINMON",
    "KXBTCMAXMON",
    "KXBTCMAX150",
)
_BTC_SERIES_TICKERS_TIER2: tuple[str, ...] = (
    "KXBTCW",
    "KXBTCMAXW",
    "BTC",
    "BTCD",
)
# C4: the active discovery/poll set is Tier-1 ONLY.  The Tier-2 series above
# return zero open contracts (confirmed empty), so polling them every 60s only
# spends read tokens against the rate limit for no contracts -- the dominant
# avoidable source of 429s.  They are retained as a named constant (and the
# static allow-list guard still checks them) so a future re-probe can promote
# any that reopen back into the active set; until then they are NOT polled.
_BTC_SERIES_TICKERS: tuple[str, ...] = _BTC_SERIES_TICKERS_TIER1

# Reconnection backoff parameters.
_RECONNECT_BASE: float = 5.0
_RECONNECT_MAX: float = 60.0
_RECONNECT_FACTOR: float = 2.0

# C4: read-budget spacing.  Kalshi Basic tier grants 200 read tokens/s and a
# GET costs 10 tokens -> a ~20 GET/s hard ceiling.  We self-throttle to <=15
# GET/s (one global gate across the concurrently-running discovery + poll
# loops, since BOTH issue GETs through _http_get) to stay clear of the ceiling.
_READ_TARGET_HZ: float = 15.0
_READ_SPACING_S: float = 1.0 / _READ_TARGET_HZ      # ~0.067 s between GETs

# C4: 429 backoff.  Kalshi sends no Retry-After header, so the wait is
# self-clocked: exponential 1 -> 2 -> 4 -> 8 ... capped at 30 s, retried a
# bounded number of times before the error propagates to the caller's existing
# per-request handling (which logs and moves to the next ticker/series).
_RATE_LIMIT_BACKOFF_BASE: float = 1.0
_RATE_LIMIT_BACKOFF_FACTOR: float = 2.0
_RATE_LIMIT_BACKOFF_MAX: float = 30.0
_RATE_LIMIT_MAX_RETRIES: int = 6
_HTTP_429_TOO_MANY_REQUESTS: int = 429


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
        recorder: FrameRecorder | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key_path = key_path
        self._key_id = key_id
        self._on_alive = on_alive
        self._queue: asyncio.Queue[PredictionMarketTick] = asyncio.Queue(maxsize=queue_maxsize)
        self._running = False
        self._private_key: Any | None = None
        self._client: httpx.AsyncClient | None = None
        # Round 9c Commit 2: optional raw-response recorder for replay.
        # None by default — opt-in via main.py's ``--record-feeds``.
        self._recorder = recorder
        # Ticker → market metadata dict (title, subtitle, close_time, …),
        # populated by _discover_markets and consumed by _poll_loop.
        self._tracked: dict[str, dict] = {}
        # C4: monotonic timestamp of the earliest moment the next GET may fire.
        # Shared across the discovery + poll loops (both go through _http_get)
        # so the combined read rate is gated to <=_READ_TARGET_HZ globally.
        self._next_get_at: float = 0.0

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

    async def _rate_gate(self) -> None:
        """Self-clocked read-budget gate (C4): space GETs to <=_READ_TARGET_HZ.

        Reserves the next slot SYNCHRONOUSLY (no await between reading the clock
        and writing ``_next_get_at``), so two concurrently-running callers
        (discovery + poll loops on the one event loop) each get a distinct,
        sequential slot — the combined GET rate stays under the limit rather
        than each loop pacing itself independently.  Then sleeps until its slot.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        scheduled = max(now, self._next_get_at)
        self._next_get_at = scheduled + _READ_SPACING_S
        delay = scheduled - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def _http_get(self, path: str) -> dict:
        """Authenticated GET, returning the JSON-decoded body.

        Fires ``on_alive`` on every 2xx response — this is what makes the
        feed register liveness even when the BTC market universe is empty.

        C4: every GET passes the read-budget gate first; a 429 (no Retry-After
        from Kalshi) triggers a self-clocked exponential backoff and retry,
        bounded by ``_RATE_LIMIT_MAX_RETRIES`` before the error propagates to
        the caller's existing per-request handling.
        """
        assert self._client is not None
        backoff = _RATE_LIMIT_BACKOFF_BASE
        attempt = 0
        while True:
            await self._rate_gate()
            headers = signed_headers("GET", path, self._private_key, self._key_id)
            resp = await self._client.get(path, headers=headers)
            if resp.status_code == _HTTP_429_TOO_MANY_REQUESTS:
                attempt += 1
                if attempt > _RATE_LIMIT_MAX_RETRIES:
                    # Exhausted: let raise_for_status raise the 429 so the
                    # caller logs + moves on (and the outer reconnect backoff
                    # engages if it is systemic).
                    resp.raise_for_status()
                logger.warning(
                    "kalshi_rate_limited",
                    path=path,
                    attempt=attempt,
                    backoff_s=round(backoff, 2),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RATE_LIMIT_BACKOFF_FACTOR, _RATE_LIMIT_BACKOFF_MAX)
                continue
            break
        resp.raise_for_status()
        # Round 9c Commit 2: record the raw response body (decompressed
        # by httpx already) before json parsing.  4xx/5xx have already
        # raised above — only successful responses reach the recorder,
        # which matches the replay-mode use case (failures are visible
        # via agent.log via structlog).  Recording happens before
        # on_alive so a recorder failure can't suppress the liveness
        # signal (recorder.record() never raises — it self-disables).
        if self._recorder is not None:
            self._recorder.record(
                DataSource.KALSHI,
                resp.content,
                datetime.now(timezone.utc),
                endpoint=path,
            )
        if self._on_alive is not None:
            self._on_alive()
        return resp.json()

    # ── Internal: discovery ───────────────────────────────────────────────────

    async def _discover_markets(self) -> None:
        """Refresh the tracked BTC contract list.

        Issues one GET per series ticker in ``_BTC_SERIES_TICKERS`` and
        union-merges the live rows into ``self._tracked``.  Replaces
        ``self._tracked`` atomically once all per-series GETs have
        completed; tickers no longer present in the response (e.g.
        closed) are dropped.

        A failed GET for one series ticker does NOT abort discovery for
        the others — the failure is logged and the remaining series
        proceed.  An empty result for a Tier-2 series is normal (see
        the allow-list comment at module scope) and not an error.
        """
        now = datetime.now(timezone.utc)
        new_tracked: dict[str, dict] = {}
        for series_ticker in _BTC_SERIES_TICKERS:
            # Path is relative to settings.kalshi_base_url, which already
            # ends in /trade-api/v2 by convention.  Do NOT prepend the API
            # prefix here — that's the doubled-path regression caught at
            # runtime in round 6 verification.
            path = (
                f"/markets"
                f"?series_ticker={series_ticker}"
                f"&status=open"
                f"&limit=200"
            )
            try:
                body = await self._http_get(path)
            except Exception as exc:
                # One series ticker's failure shouldn't break discovery
                # for the rest of the allow-list.  Log and move on; the
                # series will retry on the next 60s tick.
                logger.warning(
                    "kalshi_discovery_series_error",
                    series_ticker=series_ticker,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            markets = body.get("markets", []) or []
            for m in markets:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                # Client-side close_time prune (commit 1): a snapshot can
                # carry close_time <= now rows for up to the discovery
                # interval; filter them before they enter _tracked.
                close_time = _meta_close_time(m)
                if close_time is not None and close_time <= now:
                    continue
                # Defence-in-depth: even if a future change to the
                # allow-list accidentally lets an excluded series leak
                # in, never let the intraday/non-threshold series enter
                # _tracked.  Matches the EXCLUDED block in the
                # allow-list comment at module scope.
                if (m.get("series_ticker") or "").upper() in {
                    "KXBTC",
                    "KXBTCD",
                    "KXBTC15M",
                }:
                    continue
                new_tracked[ticker] = m
        self._tracked = new_tracked
        logger.info(
            "kalshi_discovery_completed",
            tracked=len(self._tracked),
            series_count=len(_BTC_SERIES_TICKERS),
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
                # Second close_time guard: defends against the snapshot
                # going stale within the 60s discovery interval — a
                # contract that was live at last discovery may close
                # before this poll cycle finishes.
                close_time = _meta_close_time(meta)
                if close_time is not None and close_time <= datetime.now(timezone.utc):
                    continue
                try:
                    # Path relative to base_url; see _discover_markets.
                    book_body = await self._http_get(
                        f"/markets/{ticker}/orderbook"
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
                # Wrapper key renamed from "orderbook" to "orderbook_fp"
                # in Kalshi's March 2026 fixed-point dollar-string migration.
                # The old key is gone — no dual-shape fallback (deliberate;
                # see module docstring).  Inner shape uses *_dollars keys
                # parsed by _build_tick.
                book = book_body.get("orderbook_fp") or {}
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

def _meta_close_time(meta: dict) -> datetime | None:
    """Parse the close_time on a Kalshi market dict, tz-aware UTC.

    Returns None when the field is missing or unparseable.  Callers should
    treat ``None`` as "unknown" (retain the row) rather than "expired" —
    discarding on missing data would mask upstream shape regressions.
    """
    raw = meta.get("close_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _derive_ask_depth(opposite_bid_levels: list) -> list[tuple[float, float]]:
    """Derive executable ask-side depth from the OPPOSITE side's resting bids.

    Complementary-pair pricing: a resting NO bid at price ``p`` for qty ``q``
    IS an offer to sell YES at ``1 - p`` for the same qty -- so the YES ask
    book is the NO bid book reflected through 1.0, and vice versa.  Returns
    ``[(price, size_usd), ...]`` sorted ascending (cheapest-first): the exact
    shape ``edge.fill_adjusted_price``, ``FillSimulator._evaluate_book_walk``
    and the Polymarket replay re-attach all consume.  Contract count doubles
    as the USD-notional proxy (one contract pays at most $1), matching
    ``replay._book_levels``.  Malformed levels are skipped individually --
    one bad level must not blank the whole book.

    Post-March-2026 fixed-point shape ONLY (``[price_dollars_str, qty_str]``).
    No pre-migration ``[price_cents, qty]`` frame exists anywhere on disk
    (Phase 1 audit 2026-06-11: 28,773 of 28,773 recorded orderbook frames
    across all four banked windows are ``orderbook_fp`` / ``*_dollars``), so
    there is deliberately NO legacy branch -- do not add one speculatively.

    Same-side resting-BID depth is deliberately NOT emitted: the tick's two
    ``order_book_*`` fields are ask-side executable depth by convention (the
    walkers lift cheapest-first), so mixing resting bids into them would
    corrupt the walk, and carrying bids separately needs new model + ledger
    fields -- deferred until a maker-side strategy exists to consume them.
    """
    out: list[tuple[float, float]] = []
    for level in opposite_bid_levels or []:
        try:
            price = round(1.0 - float(level[0]), 4)
            size = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if size <= 0.0:
            continue
        out.append((price, size))
    out.sort(key=lambda lvl: lvl[0])
    return out


def _build_tick(ticker: str, meta: dict, book: dict) -> PredictionMarketTick | None:
    """Combine cached market metadata with a fresh orderbook into a tick.

    Kalshi orderbook semantics (post-March-2026 fixed-point migration):
    each side has *bid* levels only under ``yes_dollars`` and
    ``no_dollars`` keys; asks are derived from the opposite side
    (``yes_ask = 1.0 - best_no_bid``).  Returns ``None`` if the
    orderbook has no bids on either side (no quotes available).

    Depth (2026-06-11 fix): the full executable ask-side book is forwarded
    as ``order_book_yes`` / ``order_book_no`` via :func:`_derive_ask_depth`.
    Before this, the levels were silently dropped at this aggregation point
    -- every Kalshi tick carried empty depth lists, so the empty-book filter
    blocked all Kalshi signals and the fill walkers walked empty books on
    the only venue we execute on.
    """
    yes_levels = book.get("yes_dollars") or []
    no_levels = book.get("no_dollars") or []

    # Each level is [price_dollars_str, qty_str] — both string-encoded
    # fixed-point floats in Kalshi's wire format.  We want the highest
    # bid on each side (best price the market is offering to pay), as
    # a float in [0, 1].
    def _best_bid_dollars(levels: list) -> float | None:
        if not levels:
            return None
        try:
            return max(float(p) for p, _ in levels)
        except (TypeError, ValueError):
            return None

    yes_best_bid = _best_bid_dollars(yes_levels)
    no_best_bid = _best_bid_dollars(no_levels)

    # Complementary-pair derivation: ask comes from the opposite side's
    # bid.  Round to 4 decimals to match the precision convention in
    # normalize_kalshi_tick / _dollar_to_prob.
    yes_best_ask = round(1.0 - no_best_bid, 4) if no_best_bid is not None else None
    no_best_ask = round(1.0 - yes_best_bid, 4) if yes_best_bid is not None else None

    if yes_best_bid is None and yes_best_ask is None:
        # No quotes at all — nothing to emit.
        return None

    raw = {
        "ticker": ticker,
        "title": meta.get("title", "") or "",
        "subtitle": meta.get("subtitle", "") or "",
        "yes_bid_dollars": yes_best_bid,
        "yes_ask_dollars": yes_best_ask,
        "no_bid_dollars": no_best_bid,
        "no_ask_dollars": no_best_ask,
        "close_time": meta.get("close_time"),
        # Executable depth: buying YES lifts the NO bids (reflected through
        # 1.0) and vice versa.  normalize_kalshi_tick passes these through
        # onto the tick verbatim.
        "order_book_yes": _derive_ask_depth(no_levels),
        "order_book_no": _derive_ask_depth(yes_levels),
    }
    return normalize_kalshi_tick(raw)
