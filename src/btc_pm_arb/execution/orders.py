"""Order manager — async state machine with Kalshi and Polymarket execution backends.

Order lifecycle::

    PENDING → PLACED → PARTIAL → FILLED
                    ↘           ↘
                     CANCELLED   CANCELLED
                    ↘
                     EXPIRED

Design notes
------------
* Both platforms implement the ``OrderExecutor`` protocol so the rest of the
  system never touches platform specifics.
* Kalshi: RSA-PSS signed REST over httpx against demo-api.kalshi.co by default.
  Fixed-point migration: API may return _dollars (float) or _fp (int, 10^-4 cents)
  depending on API version; we normalise to float [0, 1] throughout.
* Polymarket: py-clob-client GTC limit orders with EIP-712 signing.
  Read-only mode by default — orders are logged but not submitted.
* Order deduplication: every order gets a UUID client_order_id; the manager
  tracks submitted IDs and will not resubmit the same order.
* The manager is intentionally *thin*: it does not contain strategy logic,
  position tracking, or risk management. Those live in their own modules.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

from btc_pm_arb.config import settings
from btc_pm_arb.models import ArbitrageSignal, DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Order model ────────────────────────────────────────────────────────────────

class OrderState(str, Enum):
    PENDING = "pending"
    PLACED = "placed"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class Order:
    """Single order instance, updated in-place as state transitions occur."""

    client_order_id: str           # UUID we generate — deduplication key
    signal: ArbitrageSignal        # originating signal
    platform: DataSource           # KALSHI or POLYMARKET
    contract_id: str
    side: str                      # "yes" or "no"
    size_usd: float                # requested notional
    limit_price: float             # [0, 1] probability

    state: OrderState = OrderState.PENDING
    platform_order_id: str | None = None   # ID returned by the platform
    filled_size: float = 0.0               # accumulated fill
    average_fill_price: float | None = None
    fees_usd: float = 0.0

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    def transition(self, new_state: OrderState, **kwargs: Any) -> None:
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc)
        for k, v in kwargs.items():
            setattr(self, k, v)
        logger.info(
            "order.state_transition",
            client_id=self.client_order_id,
            platform=self.platform,
            contract=self.contract_id,
            side=self.side,
            state=new_state,
            filled=self.filled_size,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED}


# ── OrderExecutor protocol ─────────────────────────────────────────────────────

@runtime_checkable
class OrderExecutor(Protocol):
    """Backend-agnostic order submission interface."""

    async def submit(self, order: Order) -> None:
        """Submit order to the platform; mutate order.state in-place."""
        ...

    async def cancel(self, order: Order) -> None:
        """Request cancellation; mutate order.state in-place."""
        ...

    async def refresh(self, order: Order) -> None:
        """Poll order status; mutate order in-place with latest fill info."""
        ...


# ── Kalshi executor ────────────────────────────────────────────────────────────

class KalshiExecutor:
    """Submit GTC limit orders to Kalshi via their REST API.

    Authentication: RSA-PSS signature over ``timestamp + method + path``.
    The private key is loaded once at construction from the path in settings.
    """

    def __init__(self, dry_run: bool = True) -> None:
        self._dry_run = dry_run
        self._base_url = settings.kalshi_base_url
        self._key_id = settings.kalshi_api_key_id
        self._private_key = self._load_key()
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=10.0)

    def _load_key(self) -> Any:
        """Load RSA private key from PEM file; return None on failure."""
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            with open(settings.kalshi_private_key_path, "rb") as f:
                return load_pem_private_key(f.read(), password=None)
        except Exception as exc:
            logger.warning("kalshi.key_load_failed", error=str(exc))
            return None

    def _sign(self, method: str, path: str) -> dict[str, str]:
        """Build Kalshi auth headers using RSA-PSS."""
        ts_ms = str(int(time.time() * 1000))
        msg = (ts_ms + method.upper() + path).encode()
        if self._private_key is None:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        sig = self._private_key.sign(msg, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    @staticmethod
    def _to_cents(prob: float) -> int:
        """Convert [0, 1] probability to Kalshi cents (integer, 1–99)."""
        return max(1, min(99, round(prob * 100)))

    async def submit(self, order: Order) -> None:
        if self._dry_run:
            logger.info(
                "kalshi.dry_run_submit",
                contract=order.contract_id,
                side=order.side,
                cents=self._to_cents(order.limit_price),
                size_usd=order.size_usd,
            )
            order.transition(OrderState.PLACED, platform_order_id="dry-run-" + order.client_order_id)
            return

        path = "/markets/orders"
        headers = self._sign("POST", path)
        body = {
            "ticker": order.contract_id,
            "action": "buy",
            "side": order.side,
            "type": "limit",
            "yes_price": self._to_cents(order.limit_price) if order.side == "yes" else None,
            "no_price": self._to_cents(order.limit_price) if order.side == "no" else None,
            "count": max(1, int(order.size_usd)),
            "client_order_id": order.client_order_id,
        }
        try:
            resp = await self._client.post(path, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            platform_id = data.get("order", {}).get("order_id", "")
            order.transition(OrderState.PLACED, platform_order_id=platform_id)
        except httpx.HTTPStatusError as exc:
            logger.error("kalshi.submit_error", status=exc.response.status_code, body=exc.response.text)
            order.transition(OrderState.CANCELLED, error=str(exc))
        except Exception as exc:
            logger.error("kalshi.submit_error", error=str(exc))
            order.transition(OrderState.CANCELLED, error=str(exc))

    async def cancel(self, order: Order) -> None:
        if self._dry_run or order.platform_order_id is None:
            order.transition(OrderState.CANCELLED)
            return
        path = f"/markets/orders/{order.platform_order_id}"
        headers = self._sign("DELETE", path)
        try:
            resp = await self._client.delete(path, headers=headers)
            resp.raise_for_status()
            order.transition(OrderState.CANCELLED)
        except Exception as exc:
            logger.error("kalshi.cancel_error", error=str(exc))

    async def refresh(self, order: Order) -> None:
        if self._dry_run:
            # Simulate immediate fill in dry-run mode
            order.transition(
                OrderState.FILLED,
                filled_size=order.size_usd,
                average_fill_price=order.limit_price,
            )
            return
        if order.platform_order_id is None:
            return
        path = f"/markets/orders/{order.platform_order_id}"
        headers = self._sign("GET", path)
        try:
            resp = await self._client.get(path, headers=headers)
            resp.raise_for_status()
            data = resp.json().get("order", {})
            self._apply_kalshi_fill(order, data)
        except Exception as exc:
            logger.error("kalshi.refresh_error", error=str(exc))

    @staticmethod
    def _apply_kalshi_fill(order: Order, data: dict[str, Any]) -> None:
        """Normalise Kalshi order response — handles both _dollars and _fp fields."""
        status = data.get("status", "")
        # Kalshi may use "filled_count" (contracts) or "amount_filled" (dollars)
        filled = float(data.get("filled_count", data.get("amount_filled", 0)) or 0)
        # avg price: prefer _fp (fixed-point 10^-4 cents) then _dollars then price field
        avg_fp = data.get("avg_price_fp")
        avg_dollars = data.get("avg_price_dollars")
        avg_price_raw = data.get("avg_price", 0)
        if avg_fp is not None:
            avg_price = float(avg_fp) / 1_000_000.0   # 10^-4 cents → [0,1]
        elif avg_dollars is not None:
            avg_price = float(avg_dollars) / 100.0
        else:
            avg_price = float(avg_price_raw) / 100.0

        state_map = {
            "resting": OrderState.PLACED,
            "executed": OrderState.FILLED,
            "pending": OrderState.PENDING,
            "canceled": OrderState.CANCELLED,
            "expired": OrderState.EXPIRED,
        }
        new_state = state_map.get(status, order.state)
        order.transition(
            new_state,
            filled_size=filled,
            average_fill_price=avg_price if avg_price > 0 else order.average_fill_price,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Polymarket executor ────────────────────────────────────────────────────────

class PolymarketExecutor:
    """Submit GTC limit orders to Polymarket via py-clob-client.

    Read-only mode by default: orders are logged but not submitted.
    EIP-712 signing and nonce management are handled by the CLOB client.
    """

    def __init__(self, dry_run: bool = True) -> None:
        self._dry_run = dry_run
        self._client = self._build_client()

    def _build_client(self) -> Any:
        if not settings.polymarket_private_key:
            return None
        try:
            from py_clob_client.client import ClobClient
            return ClobClient(
                host=settings.polymarket_clob_url,
                chain_id=settings.polymarket_chain_id,
                private_key=settings.polymarket_private_key,
                signature_type=2,   # poly_gnosis_safe
                funder=None,
            )
        except Exception as exc:
            logger.warning("polymarket.client_build_failed", error=str(exc))
            return None

    async def submit(self, order: Order) -> None:
        if self._dry_run or self._client is None:
            logger.info(
                "polymarket.dry_run_submit",
                contract=order.contract_id,
                side=order.side,
                price=order.limit_price,
                size_usd=order.size_usd,
            )
            order.transition(OrderState.PLACED, platform_order_id="dry-run-" + order.client_order_id)
            return

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            args = OrderArgs(
                token_id=order.contract_id,
                price=order.limit_price,
                size=order.size_usd,
                side="BUY" if order.side == "yes" else "SELL",
                fee_rate_bps=0,
                nonce=0,
            )
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: self._client.create_and_post_order(args))
            platform_id = resp.get("orderID", "")
            order.transition(OrderState.PLACED, platform_order_id=platform_id)
        except Exception as exc:
            logger.error("polymarket.submit_error", error=str(exc))
            order.transition(OrderState.CANCELLED, error=str(exc))

    async def cancel(self, order: Order) -> None:
        if self._dry_run or self._client is None or order.platform_order_id is None:
            order.transition(OrderState.CANCELLED)
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._client.cancel(order_id=order.platform_order_id)
            )
            order.transition(OrderState.CANCELLED)
        except Exception as exc:
            logger.error("polymarket.cancel_error", error=str(exc))

    async def refresh(self, order: Order) -> None:
        if self._dry_run or self._client is None:
            order.transition(
                OrderState.FILLED,
                filled_size=order.size_usd,
                average_fill_price=order.limit_price,
            )
            return
        if order.platform_order_id is None:
            return
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None, lambda: self._client.get_order(order.platform_order_id)
            )
            status = data.get("status", "")
            filled = float(data.get("size_matched", 0) or 0)
            avg_price = float(data.get("average_price", 0) or 0)
            state_map = {
                "LIVE": OrderState.PLACED,
                "MATCHED": OrderState.FILLED,
                "CANCELED": OrderState.CANCELLED,
                "DELAYED": OrderState.PLACED,
            }
            new_state = state_map.get(status, order.state)
            order.transition(
                new_state,
                filled_size=filled,
                average_fill_price=avg_price if avg_price > 0 else order.average_fill_price,
            )
        except Exception as exc:
            logger.error("polymarket.refresh_error", error=str(exc))


# ── Order manager ──────────────────────────────────────────────────────────────

class OrderManager:
    """Owns all in-flight orders and routes to the correct backend executor.

    Usage::

        mgr = OrderManager(dry_run=True)
        order = await mgr.place(signal, size_usd=200.0)
        await mgr.refresh_all()
    """

    def __init__(self, dry_run: bool = True) -> None:
        self._dry_run = dry_run
        self._kalshi = KalshiExecutor(dry_run=dry_run)
        self._polymarket = PolymarketExecutor(dry_run=dry_run)
        self._orders: dict[str, Order] = {}          # client_order_id → Order
        self._seen_signals: set[str] = set()         # deduplication by signal fingerprint

    def _executor(self, platform: DataSource) -> OrderExecutor:
        if platform == DataSource.KALSHI:
            return self._kalshi  # type: ignore[return-value]
        return self._polymarket  # type: ignore[return-value]

    def _signal_fingerprint(self, signal: ArbitrageSignal) -> str:
        """Stable ID for a signal — prevents duplicate orders on repeated scans."""
        return (
            f"{signal.pm_quote.contract_id}:"
            f"{signal.trade_side}:"
            f"{signal.pm_quote.expiry.isoformat()}"
        )

    async def place(self, signal: ArbitrageSignal, size_usd: float) -> Order | None:
        """Create and submit an order for a signal.  Returns None if deduplicated."""
        fp = self._signal_fingerprint(signal)
        if fp in self._seen_signals:
            logger.debug("order.deduplicated", fingerprint=fp)
            return None
        self._seen_signals.add(fp)

        platform = signal.pm_quote.source
        side = "yes" if signal.trade_side == "buy_yes" else "no"
        limit_price = (
            signal.pm_quote.ask_prob if side == "yes" else
            (1.0 - signal.pm_quote.bid_prob)
        )

        order = Order(
            client_order_id=str(uuid.uuid4()),
            signal=signal,
            platform=platform,
            contract_id=signal.pm_quote.contract_id,
            side=side,
            size_usd=size_usd,
            limit_price=limit_price,
        )
        self._orders[order.client_order_id] = order

        logger.info(
            "order.placing",
            client_id=order.client_order_id,
            platform=platform,
            contract=order.contract_id,
            side=side,
            size_usd=size_usd,
            limit_price=round(limit_price, 4),
            dry_run=self._dry_run,
        )

        await self._executor(platform).submit(order)
        return order

    async def cancel(self, client_order_id: str) -> None:
        order = self._orders.get(client_order_id)
        if order is None or order.is_terminal:
            return
        await self._executor(order.platform).cancel(order)

    async def refresh_all(self) -> None:
        """Poll status of all non-terminal orders."""
        active = [o for o in self._orders.values() if not o.is_terminal]
        await asyncio.gather(*[self._executor(o.platform).refresh(o) for o in active])

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if not o.is_terminal]

    def filled_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state == OrderState.FILLED]

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    async def aclose(self) -> None:
        await self._kalshi.aclose()
