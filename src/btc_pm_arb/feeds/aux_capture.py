"""Capture-only auxiliary feed recorders for offline latency analysis.

These three streams are recorded ONLY under ``main.py --record-feeds`` and
exist purely so a later, offline analysis can measure the relative latency of:

  * "spot"     -- a fast BTC spot/index reference (Deribit ``deribit_price_index``
                  WebSocket channel), against which the laggy sources are timed.
  * "chainlink"-- the Chainlink BTC/USD aggregator round state on Polygon
                  (roundId / answer / updatedAt).  ``updatedAt`` is the
                  latency-critical on-chain push timestamp, not the price.
  * "pm5min"   -- Polymarket BTC 5-minute up/down binaries, polled at HIGH
                  frequency so their (deliberately slow) repricing lag is
                  visible in the recording.

GUARDRAIL -- capture only.  NOTHING here flows into pricing, signals, gates,
or execution.  None of these are :class:`~btc_pm_arb.models.DataSource`
members; they are bare-string recording tags.  In particular the 5-minute
contracts are recorded but NEVER added to the arbitrage strategy's
tracked/signal universe (the live ``PolymarketFeed`` discovery filter
``_is_btc_binary_threshold`` rejects them -- they carry no strike).

Each capture writes into the SAME gzipped-JSONL format the existing recorder
uses (``{base}/{tag}/{YYYY-MM-DD}/frames-HH.jsonl.gz``) via
:meth:`FrameRecorder.record`, carrying the SAME outer ``ts`` wall-clock
receive-time stamp every existing source carries.  The replay reader's k-way
merge keys on that outer ``ts``; these tags carry it so a widened recording
merges cleanly (the reader ignores the unknown tags -- see ``feeds/replay.py``).

Resilience.  All three run only as opt-in capture tasks; recording is
non-essential to live trading.  Each ``run`` loop swallows its own errors,
backs off, and -- after ``_MAX_CONSECUTIVE_FAILURES`` consecutive failures
(e.g. a Polygon RPC host not on the network allowlist) -- self-disables with a
single WARNING and returns, so a blocked stream never spams the log or takes
down the live agent.  ``asyncio.CancelledError`` always propagates for clean
shutdown.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
import structlog
import websockets

from btc_pm_arb.feeds import polymarket as _pm
from btc_pm_arb.feeds.recorder import FrameRecorder

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# -- Recording source tags (NOT DataSource members; recording-only) ------------
SOURCE_SPOT = "spot"
SOURCE_CHAINLINK = "chainlink"
SOURCE_PM5MIN = "pm5min"

# After this many consecutive failures a capture self-disables (one final
# WARNING, then returns).  Generous enough to ride out transient network blips.
_MAX_CONSECUTIVE_FAILURES = 10

# Reconnect / retry backoff (seconds) -- shared shape across captures.
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 30.0
_BACKOFF_FACTOR = 2.0


def _utc_now() -> datetime:
    """Default outer-ts clock: wall-clock UTC, matching the live feeds.

    Injectable in tests for deterministic timestamps.
    """
    return datetime.now(timezone.utc)


# -- Stream 1: fast spot reference (Deribit index WS) ---------------------------


class DeribitIndexCapture:
    """Subscribe to ``deribit_price_index.{index}`` and record every push.

    A DEDICATED WebSocket connection, separate from the trading
    :class:`~btc_pm_arb.feeds.deribit.DeribitFeed`, so the existing
    ``deribit`` recording stays byte-for-byte unchanged.  The index price is
    also embedded in every option-ticker frame the trading feed records, but
    this dedicated channel is a clean, decode-free, high-cadence spot timeline
    for the latency analysis.
    """

    def __init__(
        self,
        recorder: FrameRecorder,
        *,
        url: str,
        index_name: str = "btc_usd",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._recorder = recorder
        self._url = url
        self._index_name = index_name
        self._clock = clock
        self._channel = f"deribit_price_index.{index_name}"

    def _subscribe_payload(self) -> str:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "public/subscribe",
                "params": {"channels": [self._channel]},
            }
        )

    def _record(self, raw: str | bytes) -> None:
        """Record one raw WS frame under the ``spot`` tag with an outer ts."""
        self._recorder.record(SOURCE_SPOT, raw, self._clock())

    async def run(self, stop_event: asyncio.Event) -> None:
        backoff = _BACKOFF_BASE
        failures = 0
        logger.info("aux.spot.starting", url=self._url, channel=self._channel)
        while not stop_event.is_set():
            try:
                await self._connect_and_capture(stop_event)
                backoff = _BACKOFF_BASE
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if stop_event.is_set():
                    break
                failures += 1
                logger.warning(
                    "aux.spot.reconnecting",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    failures=failures,
                )
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning("aux.spot.disabled", failures=failures)
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
        logger.info("aux.spot.stopped")

    async def _connect_and_capture(self, stop_event: asyncio.Event) -> None:
        async with websockets.connect(
            self._url,
            ping_interval=20,
            open_timeout=15,
            close_timeout=10,
            max_size=2**20,
        ) as ws:
            await ws.send(self._subscribe_payload())
            logger.info("aux.spot.subscribed", channel=self._channel)
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self._record(raw)


# -- Stream 2: Chainlink BTC/USD round state (Polygon RPC) ----------------------

# latestRoundData() function selector (keccak256 of the signature, first 4 bytes).
_LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"


def decode_latest_round_data(result_hex: str) -> dict:
    """Decode an ABI-encoded ``latestRoundData()`` return into a plain dict.

    Returns five fields: ``round_id`` (uint80), ``answer`` (int256, signed),
    ``started_at`` / ``updated_at`` (uint256 unix seconds) and
    ``answered_in_round`` (uint80).  ``updated_at`` is the latency-critical
    on-chain push timestamp.

    Raises ``ValueError`` on a short / malformed result so the caller can
    treat it as a failed poll.
    """
    h = result_hex[2:] if result_hex.startswith("0x") else result_hex
    if len(h) < 64 * 5:
        raise ValueError(f"short latestRoundData result: {len(h)} hex chars")
    words = [h[i * 64 : (i + 1) * 64] for i in range(5)]

    def _u(w: str) -> int:
        return int(w, 16)

    def _s(w: str) -> int:
        v = int(w, 16)
        return v - (1 << 256) if v >= (1 << 255) else v

    return {
        "round_id": _u(words[0]),
        "answer": _s(words[1]),
        "started_at": _u(words[2]),
        "updated_at": _u(words[3]),
        "answered_in_round": _u(words[4]),
        # 8 decimals on the Chainlink BTC/USD aggregator -- carried so the
        # offline analysis need not hard-code it.
        "decimals": 8,
    }


class ChainlinkRoundCapture:
    """Poll the Chainlink BTC/USD aggregator's ``latestRoundData`` and record it.

    Raw JSON-RPC ``eth_call`` over httpx -- no ``web3`` dependency.  Records a
    decoded dict (round_id / answer / started_at / updated_at /
    answered_in_round) plus the raw hex result, under the ``chainlink`` tag.
    """

    def __init__(
        self,
        recorder: FrameRecorder,
        *,
        rpc_url: str,
        feed_address: str,
        interval: float = 2.0,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._recorder = recorder
        self._rpc_url = rpc_url
        self._feed_address = feed_address
        self._interval = interval
        self._clock = clock

    def _eth_call_payload(self) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": self._feed_address, "data": _LATEST_ROUND_DATA_SELECTOR},
                "latest",
            ],
        }

    def _record(self, decoded: dict, raw_hex: str) -> None:
        frame = dict(decoded)
        frame["raw_result"] = raw_hex
        frame["feed_address"] = self._feed_address
        self._recorder.record(
            SOURCE_CHAINLINK, frame, self._clock(), endpoint="latestRoundData",
        )

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(self._rpc_url, json=self._eth_call_payload())
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"rpc_error: {body['error']}")
        result = body.get("result")
        if not result or result == "0x":
            raise RuntimeError("empty eth_call result")
        decoded = decode_latest_round_data(result)
        self._record(decoded, result)

    async def run(self, stop_event: asyncio.Event) -> None:
        backoff = _BACKOFF_BASE
        failures = 0
        logger.info(
            "aux.chainlink.starting",
            rpc_url=self._rpc_url,
            feed=self._feed_address,
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            while not stop_event.is_set():
                try:
                    await self._poll_once(client)
                    failures = 0
                    backoff = _BACKOFF_BASE
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if stop_event.is_set():
                        break
                    failures += 1
                    logger.warning(
                        "aux.chainlink.poll_error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        failures=failures,
                    )
                    if failures >= _MAX_CONSECUTIVE_FAILURES:
                        logger.warning(
                            "aux.chainlink.disabled",
                            failures=failures,
                            hint=(
                                "Polygon RPC unreachable -- add the RPC host to "
                                "the network allowlist or set "
                                "chainlink_polygon_rpc_url"
                            ),
                        )
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
                    continue
                await _sleep_or_stop(stop_event, self._interval)
        logger.info("aux.chainlink.stopped")


# -- Stream 3: Polymarket BTC 5-minute up/down odds (high-frequency) ------------

# Live 5-min windows are discovered by slug prefix; the trading feed's
# threshold filter rejects them (no strike), so they are NOT in the arb universe.
_PM5_SLUG_PREFIX = "btc-updown-5m"


def is_btc_5min_updown(market: dict) -> bool:
    """True iff ``market`` is a Polymarket BTC 5-minute up/down binary.

    Keyed on the durable ``btc-updown-5m-{unix}`` slug prefix; falls back to the
    question text.  These carry outcomes ``["Up","Down"]`` (not Yes/No) and no
    strike, which is exactly why the arbitrage discovery filter excludes them.
    """
    slug = (market.get("slug") or "")
    if slug.startswith(_PM5_SLUG_PREFIX):
        return True
    q = (market.get("question") or "").lower()
    return "bitcoin up or down" in q and "5m" in slug


def resolve_up_token(market: dict) -> str | None:
    """Return the CLOB token id for the ``Up`` outcome, or None.

    Resolved by name (lower-cased ``up`` in ``outcomes``) with a fallback to
    index 0, mirroring the defensiveness of ``polymarket._resolve_yes_token``.
    """
    outcomes = _pm._coerce_to_list(market.get("outcomes"))
    token_ids = _pm._coerce_to_list(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None
    for idx, name in enumerate(outcomes):
        if str(name).strip().lower() == "up":
            return str(token_ids[idx])
    return str(token_ids[0])


class Polymarket5MinCapture:
    """Discover live BTC 5-minute up/down markets and densely record their books.

    Discovery (every ``discovery_interval``): gamma ``/markets`` filtered to
    the soonest-expiring open markets (``end_date_min=now`` so stale, never-
    closed past windows are dropped) whose slug is ``btc-updown-5m-*``.  For
    each, the ``Up`` token's CLOB ``/book`` is polled at HIGH frequency (every
    ``poll_interval``, default 1 s vs the trading feed's 5 s) so the slow
    repricing lag is visible.  Both the discovery ``/markets`` frames and the
    ``/book`` frames are recorded under the ``pm5min`` tag.
    """

    def __init__(
        self,
        recorder: FrameRecorder,
        *,
        gamma_url: str,
        clob_url: str,
        poll_interval: float = 1.0,
        discovery_interval: float = 30.0,
        max_windows: int = 4,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._recorder = recorder
        self._gamma_url = gamma_url.rstrip("/")
        self._clob_url = clob_url.rstrip("/")
        self._poll_interval = poll_interval
        self._discovery_interval = discovery_interval
        self._max_windows = max_windows
        self._clock = clock
        # token_id -> market metadata for the currently-live 5-min windows.
        self._tracked: dict[str, dict] = {}

    def _discovery_path(self) -> str:
        now_z = self._clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        return (
            "/markets?active=true&closed=false&limit=100"
            "&order=endDate&ascending=true"
            f"&end_date_min={now_z}"
        )

    def _parse_discovery(self, body: object) -> dict[str, dict]:
        rows = body if isinstance(body, list) else (
            (body.get("data") or body.get("markets") or [])
            if isinstance(body, dict) else []
        )
        tracked: dict[str, dict] = {}
        for market in rows:
            if not is_btc_5min_updown(market):
                continue
            token = resolve_up_token(market)
            if token is None:
                continue
            tracked[token] = market
            if len(tracked) >= self._max_windows:
                break
        return tracked

    async def _discover(self, client: httpx.AsyncClient) -> None:
        path = self._discovery_path()
        resp = await client.get(self._gamma_url + path)
        resp.raise_for_status()
        # Record the raw discovery frame so the offline join (token -> window)
        # is reproducible from the recording alone.
        self._recorder.record(
            SOURCE_PM5MIN, resp.content, self._clock(), endpoint=path,
        )
        self._tracked = self._parse_discovery(resp.json())
        logger.info("aux.pm5min.discovered", tracked=len(self._tracked))

    async def _poll_books(self, client: httpx.AsyncClient) -> None:
        for token_id, _meta in list(self._tracked.items()):
            path = f"/book?token_id={token_id}"
            try:
                resp = await client.get(self._clob_url + path)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "aux.pm5min.book_error",
                    token_id=token_id,
                    error=str(exc),
                )
                continue
            self._recorder.record(
                SOURCE_PM5MIN, resp.content, self._clock(), endpoint=path,
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        backoff = _BACKOFF_BASE
        failures = 0
        last_discovery = 0.0
        elapsed = 0.0
        logger.info("aux.pm5min.starting", gamma_url=self._gamma_url)
        async with httpx.AsyncClient(timeout=10.0) as client:
            while not stop_event.is_set():
                try:
                    if not self._tracked or elapsed - last_discovery >= self._discovery_interval:
                        await self._discover(client)
                        last_discovery = elapsed
                    await self._poll_books(client)
                    failures = 0
                    backoff = _BACKOFF_BASE
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if stop_event.is_set():
                        break
                    failures += 1
                    logger.warning(
                        "aux.pm5min.error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        failures=failures,
                    )
                    if failures >= _MAX_CONSECUTIVE_FAILURES:
                        logger.warning("aux.pm5min.disabled", failures=failures)
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
                    continue
                await _sleep_or_stop(stop_event, self._poll_interval)
                elapsed += self._poll_interval
        logger.info("aux.pm5min.stopped")


# -- Shared helper --------------------------------------------------------------


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds`` but wake immediately if ``stop_event`` fires."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
