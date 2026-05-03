"""Paper-settlement poller — detects Kalshi contract resolutions for paper positions.

Round 8 Commit 2.  Background asyncio task that polls Kalshi's
``GET /markets/{ticker}`` endpoint for each open paper position whose
expiry falls inside the polling window, and records terminal settlements
into the paper-trading ledger.

Why a dedicated poller (and not an extension of KalshiFeed)
-----------------------------------------------------------
``KalshiFeed._discover_markets`` (kalshi.py:250) filters
``status=open`` — settled markets disappear from discovery.  Adding a
settled-markets path there would muddy that module's contract (it
advertises live data only).  A small standalone poller keeps the
auth + JSON-shape work in one place and means the live-feed
hot path stays unchanged.

Defensive shape parsing
-----------------------
Per rev. 1 plan §f, the poller treats a market response as settled
**only** when both:

  - ``status == "settled"``
  - ``result in {"yes", "no"}``

Any other shape (e.g. ``status="closed", result=""``, or
``status="settled", result=""`` mid-resolution) logs
``paper_settlement.unexpected_shape`` at WARNING and skips — we never
record a false settlement.  This errs on the side of leaving positions
open rather than closing them with a guess.

Polling window
--------------
For each open Kalshi paper position with expiry ``T``:

  - ``T - 24h <= now <= T + 5min``  →  poll
  - ``now > T + 7d``                →  log ``paper_settlement.timeout``
                                       (position stays open)
  - otherwise                       →  skip silently

The 24h pre-window absorbs agent restart latency (catches contracts
that expired up to a day ago on next startup).  The 5min post-window
absorbs (a) wall-clock skew between local time and Kalshi's expiration
timestamps, and (b) the 60s poller cadence — tightening below ~5min
risks missing settlements on contracts that expired during a
poller-cycle gap.

Wiring (Commit 3)
-----------------
The agent provides ``get_order_record`` as a lookup into its in-memory
``PaperOrderRecord`` registry, populated as orders are placed.  This
keeps :class:`PaperPosition` free of order-side fields so its state
remains derivable purely from fill events (the load-bearing replay
invariant landed in Commit 1).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import structlog

from btc_pm_arb.execution.paper_ledger import (
    PaperLedger,
    PaperOrderRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.execution.paper_positions import PaperPosition, PaperPositionTracker
from btc_pm_arb.feeds._kalshi_auth import load_key, signed_headers
from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# How often to wake the poller (seconds).
_POLL_INTERVAL: float = 60.0
# Pre-expiry window — start polling 24h before T.
_PRE_EXPIRY_WINDOW: timedelta = timedelta(hours=24)
# Post-expiry window — keep polling for 5 min after T.
_POST_EXPIRY_WINDOW: timedelta = timedelta(minutes=5)
# Timeout — log warning when a position is unsettled this long after expiry.
_TIMEOUT: timedelta = timedelta(days=7)

# HTTP timeout (seconds) for individual market lookups.
_HTTP_TIMEOUT: float = 10.0


# ── Poller ────────────────────────────────────────────────────────────────────


class KalshiSettlementPoller:
    """Detect and record settlements for open paper positions.

    Usage::

        poller = KalshiSettlementPoller(
            tracker=paper_positions,
            ledger=paper_ledger,
            get_order_record=lambda cid: paper_orders.get(cid),
            base_url=settings.kalshi_base_url,
            key_path=settings.kalshi_private_key_path,
            key_id=settings.kalshi_api_key_id,
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(poller.run(stop_event))

    Tests inject ``http_client`` (a pre-configured httpx.AsyncClient or
    mock) and ``clock`` (a zero-arg callable returning a UTC datetime) to
    drive both the HTTP layer and the polling-window logic deterministically.
    """

    POLL_INTERVAL: float = _POLL_INTERVAL
    PRE_EXPIRY_WINDOW: timedelta = _PRE_EXPIRY_WINDOW
    POST_EXPIRY_WINDOW: timedelta = _POST_EXPIRY_WINDOW
    TIMEOUT: timedelta = _TIMEOUT

    def __init__(
        self,
        *,
        tracker: PaperPositionTracker,
        ledger: PaperLedger,
        get_order_record: Callable[[str], PaperOrderRecord | None],
        base_url: str,
        key_path: str,
        key_id: str,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tracker = tracker
        self._ledger = ledger
        self._get_order_record = get_order_record
        self._base_url = base_url.rstrip("/")
        self._key_path = key_path
        self._key_id = key_id
        # When the caller supplies http_client, the poller does not own it
        # (no aclose()).  When it constructs its own, aclose() runs in
        # :meth:`aclose`.  Tests inject a mock and set _owns_client=False.
        self._http_client = http_client
        self._owns_client = http_client is None
        self._clock = clock or _default_clock
        # Lazy-loaded private key — same pattern as KalshiFeed.  None until
        # first successful load; failure logs via load_key and the next
        # poll() returns 0 settlements.
        self._private_key: Any | None = None
        # Track which (contract_id, side) we've already logged a timeout
        # for, so we don't spam the log every 60s.
        self._timeout_logged: set[tuple[str, str]] = set()

    # ── Public lifecycle ──────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop — wakes every ``POLL_INTERVAL`` until ``stop_event`` fires."""
        logger.info("paper_settlement_poller.started", interval_s=self.POLL_INTERVAL)
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                # Single-cycle errors must not kill the poller; the next
                # cycle re-attempts.  Persistent errors will eventually
                # show up in the warning log volume.
                logger.error(
                    "paper_settlement_poller.cycle_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()),
                    timeout=self.POLL_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass  # Normal — loop and poll again
        logger.info("paper_settlement_poller.stopped")

    async def poll_once(self) -> int:
        """Single polling pass — returns the number of settlements recorded.

        Idempotent except for the side-effect of writing settlement
        records to the ledger and closing positions in the tracker.  Safe
        to call from tests in lieu of running the full :meth:`run` loop.
        """
        now = self._clock()
        open_positions = self._tracker.open_positions()

        # Dedupe by (platform, contract_id) so we make at most one HTTP
        # call per contract per cycle, then apply the same outcome to all
        # affected positions (e.g. hedged YES + NO triples).
        contracts_to_poll: dict[str, list[PaperPosition]] = {}
        for pos in open_positions:
            if pos.platform != DataSource.KALSHI:
                # Polymarket paper positions don't exist in Round 8
                # (signals are short-circuited at OrderManager.place);
                # filter defensively.
                continue
            if self._is_timeout(pos.expiry, now):
                self._log_timeout_once(pos, now)
                continue
            if not self._in_window(pos.expiry, now):
                continue
            contracts_to_poll.setdefault(pos.contract_id, []).append(pos)

        settlements_recorded = 0
        for ticker, positions in contracts_to_poll.items():
            market = await self._fetch_market(ticker)
            if market is None:
                continue
            settled = self._parse_settlement(market, ticker)
            if settled is None:
                continue
            settlement_price, raw_result = settled
            for pos in positions:
                self._record_settlement(pos, settlement_price, raw_result, now)
                settlements_recorded += 1
        return settlements_recorded

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ── Window logic ──────────────────────────────────────────────────────────

    def _in_window(self, expiry: datetime, now: datetime) -> bool:
        return (expiry - self.PRE_EXPIRY_WINDOW) <= now <= (expiry + self.POST_EXPIRY_WINDOW)

    def _is_timeout(self, expiry: datetime, now: datetime) -> bool:
        return (now - expiry) > self.TIMEOUT

    def _log_timeout_once(self, pos: PaperPosition, now: datetime) -> None:
        key = (pos.contract_id, pos.side)
        if key in self._timeout_logged:
            return
        self._timeout_logged.add(key)
        logger.warning(
            "paper_settlement.timeout",
            contract=pos.contract_id,
            platform=pos.platform.value,
            side=pos.side,
            expiry=pos.expiry.isoformat(),
            days_overdue=round((now - pos.expiry).total_seconds() / 86400.0, 2),
        )

    # ── HTTP layer ────────────────────────────────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient | None:
        if self._http_client is not None:
            return self._http_client
        # Construct on first use.  load_key may fail (returns None and logs);
        # in that case _http_get returns None and the poller produces zero
        # settlements until the key issue is resolved.
        if self._private_key is None:
            self._private_key = load_key(self._key_path)
        self._http_client = httpx.AsyncClient(
            base_url=self._base_url, timeout=_HTTP_TIMEOUT
        )
        return self._http_client

    async def _fetch_market(self, ticker: str) -> dict | None:
        """Return the inner market dict for ``ticker``, or None on any failure.

        Defensive: any HTTP error or malformed envelope is logged at
        WARNING and yields None.  The caller treats None as "no signal
        this cycle" — never as "settled".
        """
        client = self._ensure_client()
        if client is None:
            return None
        # Lazy-load the key for self-owned clients; tests inject a mock
        # client and skip auth entirely.
        if self._owns_client and self._private_key is None:
            self._private_key = load_key(self._key_path)
        path = f"/markets/{ticker}"
        try:
            if self._owns_client:
                headers = signed_headers(
                    "GET", path, self._private_key, self._key_id
                )
            else:
                # Test path — no auth signing required against the mock.
                headers = {}
            resp = await client.get(path, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            logger.warning(
                "paper_settlement.fetch_error",
                ticker=ticker,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        # Kalshi wraps the per-market response under "market"; if the
        # endpoint shape changes upstream, accept the bare dict too.
        if isinstance(body, dict) and "market" in body:
            inner = body["market"]
            if isinstance(inner, dict):
                return inner
        if isinstance(body, dict):
            return body
        logger.warning(
            "paper_settlement.unexpected_envelope",
            ticker=ticker,
            envelope_type=type(body).__name__,
        )
        return None

    # ── Settlement detection ──────────────────────────────────────────────────

    def _parse_settlement(
        self, market: dict, ticker: str
    ) -> tuple[float, str] | None:
        """Return ``(settlement_price, raw_result)`` if settled, else ``None``.

        The defensive contract: BOTH ``status == "settled"`` AND
        ``result in {"yes", "no"}`` must hold.  Anything else logs
        ``paper_settlement.unexpected_shape`` and yields None — the caller
        treats this as "not settled this cycle", not "settled but
        unparseable".
        """
        status = market.get("status", "")
        result = market.get("result", "")
        if status != "settled" or result not in ("yes", "no"):
            # Only log for the suggestive partial-match cases — full
            # "open"/"closed" status spam isn't useful at INFO/WARNING.
            if status == "settled" or result in ("yes", "no"):
                logger.warning(
                    "paper_settlement.unexpected_shape",
                    ticker=ticker,
                    status=status,
                    result=result,
                )
            return None
        settlement_price = 1.0 if result == "yes" else 0.0
        return settlement_price, result

    def _record_settlement(
        self,
        pos: PaperPosition,
        settlement_price: float,
        raw_result: str,
        now: datetime,
    ) -> None:
        """Build a settlement record, append it, and close the position.

        Pulls ``theoretical_edge`` from the originating order via
        ``get_order_record(order_ids[0])``.  If the order can't be looked
        up (orphan position — should be impossible in practice), logs
        ``paper_settlement.missing_order_record`` and uses 0.0 — the
        record still gets written so the position closes and the JSONL
        stream stays consistent.
        """
        payout_price = settlement_price if pos.side == "yes" else 1.0 - settlement_price
        realized_pnl = (payout_price - pos.entry_price) * pos.filled_size_usd
        if realized_pnl > 1e-4:
            outcome = "win"
        elif realized_pnl < -1e-4:
            outcome = "loss"
        else:
            outcome = "push"

        # Look up theoretical_edge from the originating order.  Default to
        # 0.0 if the order registry doesn't have it (defensive — should
        # not happen in production with the Commit-3 wiring).
        theoretical_edge = 0.0
        client_order_id = pos.order_ids[0] if pos.order_ids else ""
        if client_order_id:
            order_record = self._get_order_record(client_order_id)
            if order_record is not None:
                theoretical_edge = order_record.adjusted_edge
            else:
                logger.warning(
                    "paper_settlement.missing_order_record",
                    contract=pos.contract_id,
                    client_order_id=client_order_id,
                )

        record = PaperSettlementRecord(
            client_order_id=client_order_id,
            contract_id=pos.contract_id,
            platform=pos.platform,
            side=pos.side,  # type: ignore[arg-type]
            settled_at=now,
            settlement_price=settlement_price,
            payout_price=payout_price,
            entry_price=pos.entry_price,
            size_usd=pos.filled_size_usd,
            realized_pnl=realized_pnl,
            fees_usd=pos.fees_usd,
            outcome=outcome,  # type: ignore[arg-type]
            theoretical_edge=theoretical_edge,
            expiry=pos.expiry,
        )
        self._ledger.append_settlement(record)
        self._tracker.settle(record)

        # Mirror the existing settlement.recorded log shape from
        # execution/settlement.py for operator parity.
        logger.info(
            "paper_settlement.recorded",
            contract=pos.contract_id,
            platform=pos.platform.value,
            side=pos.side,
            outcome=outcome,
            settlement_price=settlement_price,
            entry_price=round(pos.entry_price, 4),
            realized_pnl=round(realized_pnl, 4),
            theoretical_edge=round(theoretical_edge, 4),
            raw_result=raw_result,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)
