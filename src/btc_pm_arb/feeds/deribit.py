"""Deribit WebSocket feed — subscribes to BTC option tickers.

Protocol: JSON-RPC 2.0 over WebSocket.
Endpoint: wss://www.deribit.com/ws/api/v2 (public, no auth required).

Flow:
  1. Connect.
  2. Fetch all active BTC option instruments via public/get_instruments.
  3. Subscribe to ticker.{instrument}.100ms for each instrument in batches.
     (Public throttled channel — no auth required. Tick-level data is not
     needed because PM/Kalshi prices refresh every few seconds.)
  4. Parse subscription messages and emit OptionTick objects to callers via
     an asyncio.Queue.
  5. Send heartbeats every 15 s; handle server-side heartbeat requests.
  6. On disconnect: exponential-backoff reconnect (1 s → 2 s → 4 s … 60 s cap).

Instrument name format (Deribit): BTC-DDMMMYY-STRIKE-C/P
  e.g.  BTC-26APR24-50000-C
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import structlog
import websockets

from btc_pm_arb.feeds.recorder import FrameRecorder
from btc_pm_arb.models import DataSource, Greeks, OptionTick, OptionType

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Deribit instrument names use a 3-letter abbreviated month with leading
# zero-padded day: e.g. 26APR24, 5JAN25.
_INSTRUMENT_RE = re.compile(
    r"^BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-(C|P)$"
)
_MONTH_MAP: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Deribit option expiries settle at 08:00 UTC.
_EXPIRY_HOUR_UTC = 8

# Subscription batch size — Deribit allows large channel lists but we stay
# conservative to avoid hitting message-size limits.
_SUBSCRIBE_BATCH = 200

# Heartbeat interval (seconds).  Deribit drops idle connections after ~60 s.
_HEARTBEAT_INTERVAL = 15

# Reconnection back-off parameters.
_RECONNECT_BASE = 1.0   # seconds
_RECONNECT_MAX = 60.0   # seconds cap
_RECONNECT_FACTOR = 2.0


# ── Helper: version-tolerant websocket "is open?" check ───────────────────────

def _ws_open(ws: Any) -> bool:
    """Return True iff ``ws`` is connected and OPEN.

    Spans both the pre-13 and post-13 ``websockets`` library APIs:
      * websockets <13 exposed a boolean ``.closed`` property.
      * websockets >=13 removed ``.closed`` in favour of ``.state`` (a
        ``websockets.protocol.State`` enum: CONNECTING / OPEN / CLOSING / CLOSED).
    Without this shim, calls like ``ws.closed`` on >=13 raise AttributeError
    inside the heartbeat loop, taking the connection down every ~80 s.
    """
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    if state is not None:
        return getattr(state, "name", "") == "OPEN"
    # Fallback for pre-13 websockets that exposed ``.closed``
    return not getattr(ws, "closed", True)


# ── Helper: parse instrument name ─────────────────────────────────────────────

def parse_instrument(name: str) -> tuple[float, datetime, OptionType] | None:
    """Return (strike, expiry_utc, option_type) parsed from a Deribit BTC
    option instrument name, or None if the name doesn't match."""
    m = _INSTRUMENT_RE.match(name)
    if not m:
        return None
    day, month_str, year2, strike_str, kind = m.groups()
    month = _MONTH_MAP.get(month_str)
    if month is None:
        return None
    year = 2000 + int(year2)
    expiry = datetime(year, month, int(day), _EXPIRY_HOUR_UTC, 0, 0, tzinfo=timezone.utc)
    strike = float(strike_str)
    option_type = OptionType.CALL if kind == "C" else OptionType.PUT
    return strike, expiry, option_type


# ── Main feed class ───────────────────────────────────────────────────────────

class DeribitFeed:
    """Async Deribit BTC options WebSocket feed.

    Usage::

        feed = DeribitFeed(url="wss://www.deribit.com/ws/api/v2")
        async with feed:
            async for tick in feed.ticks():
                process(tick)

    The feed maintains a single WebSocket connection and re-subscribes
    automatically on reconnect.
    """

    def __init__(
        self,
        url: str = "wss://www.deribit.com/ws/api/v2",
        queue_maxsize: int = 10_000,
        recorder: FrameRecorder | None = None,
    ) -> None:
        self._url = url
        self._queue: asyncio.Queue[OptionTick] = asyncio.Queue(maxsize=queue_maxsize)
        self._running = False
        # Round 9c Commit 2: optional raw-frame recorder for replay-mode
        # validation.  None by default — recording is opt-in via main.py's
        # ``--record-feeds`` flag.
        self._recorder = recorder
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._rpc_id = 0
        self._pending_rpcs: dict[int, asyncio.Future[Any]] = {}
        # Companion to _pending_rpcs: maps the same rpc_id to its method
        # name, so RPC-response dispatch logs can name the original method
        # (responses don't carry method info themselves).
        self._pending_rpc_methods: dict[int, str] = {}
        # Cache of parsed instrument metadata to avoid re-parsing on every tick
        self._instrument_cache: dict[str, tuple[float, datetime, OptionType]] = {}
        # Strong references to short-lived background tasks (heartbeat replies)
        # so the runtime cannot GC them mid-flight.  Tasks self-discard on done.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # Ids of public/test calls we issued as heartbeat replies (sent
        # directly via _ws.send, NOT through _rpc).  Deribit acks these
        # with a normal JSON-RPC response — tracked here so the dispatch
        # log can distinguish "expected heartbeat ack" from "real
        # unmatched response" (the latter being a true diagnostic signal).
        self._heartbeat_reply_ids: set[int] = set()
        # ── Diagnostic counters (instrumentation; no behavioural effect) ─────
        # Reset on each successful connect (in _connect_and_run).
        self._frames_processed: int = 0
        self._rpc_responses_dispatched: int = 0
        self._rpc_responses_unmatched: int = 0
        self._rpc_responses_late: int = 0
        self._heartbeats_received: int = 0
        self._loop_alive_log_ts: float = 0.0

    # ── Public interface ──────────────────────────────────────────────────────

    async def __aenter__(self) -> "DeribitFeed":
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="deribit-feed")
        return self

    async def __aexit__(self, *_: object) -> None:
        self._running = False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def ticks(self) -> AsyncIterator[OptionTick]:
        """Yield OptionTick objects as they arrive from Deribit."""
        while self._running:
            try:
                tick = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield tick
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue

    # ── Internal: connection loop ─────────────────────────────────────────────

    async def _run_forever(self) -> None:
        backoff = _RECONNECT_BASE
        while self._running:
            try:
                await self._connect_and_run()
                # Clean exit (shouldn't happen unless stopped)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "deribit_disconnected",
                    error=str(exc),
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX)
            else:
                backoff = _RECONNECT_BASE  # reset on clean reconnect

    async def _connect_and_run(self) -> None:
        logger.info("deribit_connecting", url=self._url)
        async with websockets.connect(
            self._url,
            ping_interval=None,   # we manage our own heartbeat
            open_timeout=20,
            close_timeout=10,
            max_size=2**23,       # 8 MiB — instrument list can be large
        ) as ws:
            self._ws = ws
            self._pending_rpcs.clear()
            self._pending_rpc_methods.clear()
            self._heartbeat_reply_ids.clear()
            # Reset diagnostic counters so each connect-cycle's logs are
            # independently interpretable.
            self._frames_processed = 0
            self._rpc_responses_dispatched = 0
            self._rpc_responses_unmatched = 0
            self._rpc_responses_late = 0
            self._heartbeats_received = 0
            self._loop_alive_log_ts = 0.0
            logger.info("deribit_connected")

            # The message loop must be running BEFORE we issue any RPC so that
            # responses get dispatched to the awaiting Future.  Run setup
            # (fetch_instruments + subscribe) and the heartbeat concurrently
            # with the message loop.
            async def _setup_then_heartbeat() -> None:
                instruments = await self._fetch_instruments()
                logger.info("deribit_instruments_fetched", count=len(instruments))
                await self._subscribe(instruments)
                await self._heartbeat_loop()

            await asyncio.gather(
                self._message_loop(),
                _setup_then_heartbeat(),
            )

    # ── Internal: RPC helpers ─────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and await its response."""
        if self._ws is None:
            raise RuntimeError("Not connected")
        rpc_id = self._next_id()
        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending_rpcs[rpc_id] = fut
        self._pending_rpc_methods[rpc_id] = method
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params,
        })
        t_send = time.monotonic()
        logger.debug(
            "deribit_rpc_sent",
            rpc_id=rpc_id,
            rpc_id_type=type(rpc_id).__name__,
            method=method,
        )
        await self._ws.send(payload)
        try:
            result = await asyncio.wait_for(fut, timeout=30.0)
            elapsed_ms = round((time.monotonic() - t_send) * 1000, 1)
            logger.debug(
                "deribit_rpc_completed",
                rpc_id=rpc_id,
                method=method,
                elapsed_ms=elapsed_ms,
            )
            return result
        except asyncio.TimeoutError:
            elapsed_ms = round((time.monotonic() - t_send) * 1000, 1)
            self._pending_rpcs.pop(rpc_id, None)
            self._pending_rpc_methods.pop(rpc_id, None)
            # Snapshot the message-loop state at the moment of timeout so
            # we can tell whether the response was missed (loop alive but
            # not dispatching) or the loop is hung (no frames processed).
            logger.warning(
                "deribit_rpc_timeout_diagnostic",
                rpc_id=rpc_id,
                rpc_id_type=type(rpc_id).__name__,
                method=method,
                elapsed_ms=elapsed_ms,
                pending_keys_at_timeout=sorted(self._pending_rpcs.keys()),
                frames_processed=self._frames_processed,
                rpc_responses_dispatched=self._rpc_responses_dispatched,
                rpc_responses_unmatched=self._rpc_responses_unmatched,
                rpc_responses_late=self._rpc_responses_late,
                heartbeats_received=self._heartbeats_received,
            )
            raise TimeoutError(f"RPC {method} timed out after 30 s")

    # ── Internal: instrument discovery ───────────────────────────────────────

    async def _fetch_instruments(self) -> list[str]:
        """Return names of all non-expired BTC option instruments."""
        result = await self._rpc(
            "public/get_instruments",
            {"currency": "BTC", "kind": "option", "expired": False},
        )
        names: list[str] = []
        for item in result:
            name: str = item["instrument_name"]
            parsed = parse_instrument(name)
            if parsed is not None:
                self._instrument_cache[name] = parsed
                names.append(name)
        return names

    # ── Internal: subscription ────────────────────────────────────────────────

    async def _subscribe(self, instruments: list[str]) -> None:
        """Subscribe to ticker.{instrument}.100ms in batches.

        We use the public throttled `.100ms` variant rather than `.raw`
        because `.raw` requires an authenticated session (Deribit error
        13778: raw_subscriptions_not_available_for_unauthorized).  100 ms
        updates are more than sufficient — PM/Kalshi prices refresh on
        the order of seconds.
        """
        channels = [f"ticker.{name}.100ms" for name in instruments]
        for i in range(0, len(channels), _SUBSCRIBE_BATCH):
            batch = channels[i : i + _SUBSCRIBE_BATCH]
            await self._rpc("public/subscribe", {"channels": batch})
            logger.debug(
                "deribit_subscribed_batch",
                offset=i,
                batch_size=len(batch),
            )
        logger.info("deribit_subscriptions_complete", total=len(channels))

    # ── Internal: message loop ────────────────────────────────────────────────

    async def _message_loop(self) -> None:
        assert self._ws is not None
        loop_started = time.monotonic()
        try:
            async for raw in self._ws:
                # Round 9c Commit 2: record the raw wire frame before any
                # parsing so a JSONDecodeError still leaves the original
                # payload on disk for forensic replay.  No-op when
                # self._recorder is None (the default).
                if self._recorder is not None:
                    self._recorder.record(
                        DataSource.DERIBIT, raw, datetime.now(timezone.utc),
                    )
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("deribit_invalid_json", raw=raw[:200])
                    continue
                await self._handle_message(msg)
                self._frames_processed += 1
                # Periodic liveness log — fires only when the loop is
                # actually iterating frames.  If RPC timeouts occur and the
                # last `deribit_message_loop_alive` was >5 s ago, the loop
                # is hung; if these logs are still firing through a timeout,
                # the loop is alive and the bug is elsewhere.
                now = time.monotonic()
                if now - self._loop_alive_log_ts >= 5.0:
                    logger.debug(
                        "deribit_message_loop_alive",
                        frames=self._frames_processed,
                        rpc_responses_dispatched=self._rpc_responses_dispatched,
                        rpc_responses_unmatched=self._rpc_responses_unmatched,
                        heartbeats_received=self._heartbeats_received,
                        pending_rpcs=sorted(self._pending_rpcs.keys()),
                        seconds_since_connect=round(now - loop_started, 2),
                    )
                    self._loop_alive_log_ts = now
                # Force a single trip through the event loop after each frame.
                # See diagnostic round 5 — kept while we instrument; may be
                # revisited once we understand why prior fixes didn't help.
                await asyncio.sleep(0)
            logger.info(
                "deribit_message_loop_exit",
                reason="async_for_returned",
                frames=self._frames_processed,
                seconds_since_connect=round(time.monotonic() - loop_started, 2),
            )
        except Exception as exc:
            logger.warning(
                "deribit_message_loop_exit",
                reason="exception",
                error_type=type(exc).__name__,
                error=str(exc),
                frames=self._frames_processed,
                seconds_since_connect=round(time.monotonic() - loop_started, 2),
            )
            raise

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        # JSON-RPC response to one of our requests
        if "id" in msg:
            rpc_id = msg["id"]
            # First: is this the server's ack of one of our heartbeat
            # replies (sent fire-and-forget via _respond_to_heartbeat)?
            # Those are expected and carry no diagnostic value.
            if isinstance(rpc_id, int) and rpc_id in self._heartbeat_reply_ids:
                self._heartbeat_reply_ids.discard(rpc_id)
                logger.debug("deribit_heartbeat_reply_ack", response_id=rpc_id)
                return
            sent_method = self._pending_rpc_methods.pop(rpc_id, None)
            fut = self._pending_rpcs.pop(rpc_id, None)
            if fut is None:
                # Response arrived for an id we are not tracking.  Either
                # an id-type mismatch (int vs str), a duplicate id, or a
                # stale response from a prior connection.  Diagnostic dump
                # logs the response id's runtime type and our pending keys.
                self._rpc_responses_unmatched += 1
                logger.warning(
                    "deribit_rpc_response_unmatched",
                    response_id=rpc_id,
                    response_id_type=type(rpc_id).__name__,
                    pending_keys=sorted(
                        self._pending_rpcs.keys(),
                        key=lambda k: (isinstance(k, str), k),
                    ),
                    pending_key_types=sorted(
                        {type(k).__name__ for k in self._pending_rpcs.keys()}
                    ),
                    has_error="error" in msg,
                )
            elif fut.done():
                # Response arrived after the awaiting task had already
                # given up (timeout/cancel).  Logged so we can correlate.
                self._rpc_responses_late += 1
                logger.warning(
                    "deribit_rpc_response_late",
                    response_id=rpc_id,
                    method=sent_method,
                    has_error="error" in msg,
                )
            else:
                self._rpc_responses_dispatched += 1
                logger.debug(
                    "deribit_rpc_response_dispatched",
                    response_id=rpc_id,
                    method=sent_method,
                    has_error="error" in msg,
                )
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(
                        RuntimeError(f"RPC error {err.get('code')}: {err.get('message')}")
                    )
                else:
                    fut.set_result(msg.get("result"))
            return

        # Server-push notification
        method = msg.get("method")
        if method == "subscription":
            params = msg.get("params", {})
            channel: str = params.get("channel", "")
            data = params.get("data", {})
            # We only subscribe to ticker.* channels, so any ticker
            # notification (regardless of suffix variant — .100ms, .raw,
            # etc.) routes to the ticker handler.
            if channel.startswith("ticker."):
                self._handle_ticker(data)
        elif method == "heartbeat":
            # Server-initiated heartbeat.  Spawn the reply as a background
            # task so the message loop continues reading frames (and
            # dispatching RPC responses) while our reply is in flight on
            # the wire.  Stash the task so it can't be GC'd mid-send;
            # auto-discard on completion — no further lifecycle management.
            self._heartbeats_received += 1
            params = msg.get("params", {}) or {}
            logger.debug(
                "deribit_heartbeat_received",
                hb_type=params.get("type", "?"),
                count=self._heartbeats_received,
            )
            task = asyncio.create_task(self._respond_to_heartbeat())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    def _handle_ticker(self, data: dict[str, Any]) -> None:
        """Parse a raw ticker payload and push an OptionTick onto the queue."""
        name: str | None = data.get("instrument_name")
        if not name:
            return

        parsed = self._instrument_cache.get(name) or parse_instrument(name)
        if parsed is None:
            return
        if name not in self._instrument_cache:
            self._instrument_cache[name] = parsed

        strike, expiry, option_type = parsed

        raw_greeks = data.get("greeks") or {}
        greeks = Greeks(
            delta=raw_greeks.get("delta", 0.0),
            gamma=raw_greeks.get("gamma", 0.0),
            vega=raw_greeks.get("vega", 0.0),
            theta=raw_greeks.get("theta", 0.0),
            rho=raw_greeks.get("rho", 0.0),
        ) if raw_greeks else None

        ts_ms: int = data.get("timestamp", int(time.time() * 1000))
        timestamp = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        # underlying_price is the Deribit index price used in the tick
        underlying_price: float = data.get("underlying_price") or data.get("index_price", 0.0)
        index_price: float = data.get("index_price") or underlying_price
        if underlying_price == 0.0 or index_price == 0.0:
            # Can't use this tick without a reference price
            return

        tick = OptionTick(
            instrument_name=name,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            bid=data.get("best_bid_price") or None,
            ask=data.get("best_ask_price") or None,
            mark_price=data.get("mark_price", 0.0),
            bid_iv=data.get("bid_iv") or None,
            ask_iv=data.get("ask_iv") or None,
            mark_iv=data.get("mark_iv") or None,
            greeks=greeks,
            underlying_price=underlying_price,
            index_price=index_price,
            open_interest=data.get("open_interest", 0.0),
            timestamp=timestamp,
        )

        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            # Drop the oldest tick to make room — latency > completeness here
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(tick)
            except asyncio.QueueFull:
                logger.warning("deribit_queue_full_drop", instrument=name)

    # ── Internal: heartbeat ───────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Configure Deribit's server-initiated heartbeats and idle.

        Once ``public/set_heartbeat`` is configured, Deribit periodically
        pushes ``test_request`` notifications and we reply via
        :meth:`_respond_to_heartbeat` (dispatched from the message loop as
        a background task).  No client-side periodic ``public/test`` probe
        is needed — the server's own protocol is sufficient for liveness.

        An earlier version of this loop sent ``public/test`` every
        ``_HEARTBEAT_INTERVAL`` seconds in addition to relying on the
        server-initiated heartbeats.  That probe was redundant noise and
        complicated diagnosis during round 4–5 (its 30 s wait_for timeout
        was an early symptom of the real underlying event-loop starvation
        bug, not a transport-layer issue).  Round 6 / Fix C removes it.
        """
        assert self._ws is not None
        try:
            await self._rpc(
                "public/set_heartbeat",
                {"interval": _HEARTBEAT_INTERVAL},
            )
        except Exception as exc:
            logger.warning("deribit_heartbeat_setup_failed", error=str(exc))
            return

        # Idle until shutdown or disconnect.  Server-initiated test_requests
        # arriving via the message loop are responded to in
        # _respond_to_heartbeat; this loop merely keeps a task alive so
        # _setup_then_heartbeat in _connect_and_run doesn't return early.
        while self._running:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            if not _ws_open(self._ws):
                break

    async def _respond_to_heartbeat(self) -> None:
        """Respond to a server-initiated heartbeat/test_request."""
        if not _ws_open(self._ws):
            return
        try:
            rpc_id = self._next_id()
            # Register the id so _handle_message can recognise the
            # incoming server ack as expected heartbeat-reply traffic
            # rather than logging it as a true unmatched response.
            self._heartbeat_reply_ids.add(rpc_id)
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "public/test",
                "params": {},
            })
            await self._ws.send(payload)
        except Exception as exc:
            self._heartbeat_reply_ids.discard(rpc_id)
            logger.warning("deribit_heartbeat_respond_failed", error=str(exc))
