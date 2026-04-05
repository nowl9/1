"""Deribit WebSocket feed — subscribes to BTC option tickers.

Protocol: JSON-RPC 2.0 over WebSocket.
Endpoint: wss://www.deribit.com/ws/api/v2 (public, no auth required).

Flow:
  1. Connect.
  2. Fetch all active BTC option instruments via public/get_instruments.
  3. Subscribe to ticker.{instrument}.raw for each instrument in batches.
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
from websockets.exceptions import ConnectionClosed

from btc_pm_arb.models import Greeks, OptionTick, OptionType

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
    ) -> None:
        self._url = url
        self._queue: asyncio.Queue[OptionTick] = asyncio.Queue(maxsize=queue_maxsize)
        self._running = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._rpc_id = 0
        self._pending_rpcs: dict[int, asyncio.Future[Any]] = {}
        # Cache of parsed instrument metadata to avoid re-parsing on every tick
        self._instrument_cache: dict[str, tuple[float, datetime, OptionType]] = {}

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
            logger.info("deribit_connected")

            # Fetch instruments, subscribe, then pump messages.
            instruments = await self._fetch_instruments()
            logger.info("deribit_instruments_fetched", count=len(instruments))

            await self._subscribe(instruments)

            await asyncio.gather(
                self._message_loop(),
                self._heartbeat_loop(),
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
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params,
        })
        await self._ws.send(payload)
        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending_rpcs.pop(rpc_id, None)
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
        """Subscribe to ticker.{instrument}.raw in batches."""
        channels = [f"ticker.{name}.raw" for name in instruments]
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
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("deribit_invalid_json", raw=raw[:200])
                continue
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        # JSON-RPC response to one of our requests
        if "id" in msg:
            rpc_id: int = msg["id"]
            fut = self._pending_rpcs.pop(rpc_id, None)
            if fut and not fut.done():
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
            if channel.startswith("ticker.") and channel.endswith(".raw"):
                self._handle_ticker(data)
        elif method == "heartbeat":
            # Server-initiated heartbeat — respond with test_request
            await self._respond_to_heartbeat()

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
        """Send set_heartbeat every HEARTBEAT_INTERVAL seconds."""
        assert self._ws is not None
        # Enable server heartbeats so we detect stale connections quickly.
        try:
            await self._rpc(
                "public/set_heartbeat",
                {"interval": _HEARTBEAT_INTERVAL},
            )
        except Exception as exc:
            logger.warning("deribit_heartbeat_setup_failed", error=str(exc))
            return

        while self._running:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            if self._ws is None or self._ws.closed:
                break
            # test_request triggers a heartbeat response from the server;
            # if it doesn't respond the connection dies and we reconnect.
            try:
                await self._rpc("public/test", {})
            except (ConnectionClosed, TimeoutError) as exc:
                logger.warning("deribit_heartbeat_failed", error=str(exc))
                break

    async def _respond_to_heartbeat(self) -> None:
        """Respond to a server-initiated heartbeat/test_request."""
        if self._ws is None or self._ws.closed:
            return
        try:
            rpc_id = self._next_id()
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "public/test",
                "params": {},
            })
            await self._ws.send(payload)
        except Exception as exc:
            logger.warning("deribit_heartbeat_respond_failed", error=str(exc))
