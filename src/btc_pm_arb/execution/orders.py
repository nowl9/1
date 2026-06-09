"""Order manager — async state machine for Kalshi-only execution.

Order lifecycle::

    PENDING → PLACED → PARTIAL → FILLED
                    ↘           ↘
                     CANCELLED   CANCELLED
                    ↘
                     EXPIRED

Design notes
------------
* Kalshi is the **only** execution venue.  Kalshi: RSA-PSS signed REST over
  httpx against demo-api.kalshi.co by default.  Fixed-point migration:
  API may return _dollars (float) or _fp (int, 10^-4 cents) depending on API
  version; we normalise to float [0, 1] throughout.
* Polymarket has no LIVE execution (US-restricted; trading is geoblocked).
  In LIVE mode (``dry_run_paper_mode=False``) signals whose
  ``pm_quote.source == DataSource.POLYMARKET`` are still logged as
  ``signal.polymarket_data_only`` in ``OrderManager.place()`` and never
  reach any executor -- the live-trading guardrail is untouched.
  In PAPER mode (``dry_run_paper_mode=True``) that drop is lifted (build
  step 2, plan sections 3.2 / 4.1): PM orders route to the venue-agnostic
  :class:`PaperExecutor` (which submits nothing, just flips PENDING ->
  PLACED) and into the SAME paper :class:`fill_simulator.FillSimulator`
  Kalshi uses.  PM clears the identical 12 gates upstream in the signal
  filter -- the un-short-circuit skips none of them.
  ``PolymarketExecutor`` survives as a disabled stub for legacy imports.
* Order deduplication: every order gets a UUID client_order_id; the manager
  tracks submitted IDs and will not resubmit the same order.
* The manager is intentionally *thin*: it does not contain strategy logic,
  position tracking, or risk management. Those live in their own modules.
"""

from __future__ import annotations

import asyncio
import hashlib
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

    Round 8 paper-mode flag (``dry_run_paper_mode``)
    -------------------------------------------------
    When ``dry_run_paper_mode=True`` (only meaningful with ``dry_run=True``),
    :meth:`submit` still flips the order PENDING → PLACED so order-lifecycle
    consumers see consistent state, but :meth:`refresh` becomes a no-op —
    the paper :class:`fill_simulator.FillSimulator` owns the FILLED
    transition.  Default is False so existing callers (and the existing
    test_integration.py suite) continue to see the optimistic instant-fill
    behaviour on ``refresh()``.
    """

    def __init__(
        self, dry_run: bool = True, dry_run_paper_mode: bool = False
    ) -> None:
        self._dry_run = dry_run
        self._dry_run_paper_mode = dry_run_paper_mode
        self._base_url = settings.kalshi_base_url
        self._key_id = settings.kalshi_api_key_id
        # Shared with feeds.kalshi.KalshiFeed via feeds._kalshi_auth so
        # signing logic has exactly one source of truth.
        from btc_pm_arb.feeds._kalshi_auth import load_key as _load_kalshi_key
        self._private_key = _load_kalshi_key(settings.kalshi_private_key_path)
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=10.0)

    def _sign(self, method: str, path: str) -> dict[str, str]:
        """Build Kalshi auth headers using RSA-PSS (delegated to shared helper)."""
        from btc_pm_arb.feeds._kalshi_auth import signed_headers
        return signed_headers(method, path, self._private_key, self._key_id)

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
            if self._dry_run_paper_mode:
                # Paper mode (Round 8): the FillSimulator owns the FILLED
                # transition.  Refresh is intentionally a no-op so the
                # executor doesn't double-fill orders the simulator has
                # already evaluated.  See fill_simulator.py module docstring.
                return
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


# ── Polymarket executor (DISABLED — US-restricted, data-only) ─────────────────

class PolymarketExecutor:
    """Disabled stub.  Polymarket trading is geoblocked for US users.

    The class is kept so legacy imports (e.g. in tests) continue to resolve,
    but every method raises ``RuntimeError``.  Polymarket signals are
    intercepted by ``OrderManager.place()`` and never reach an executor — so
    these methods should never be called in practice.  If they are, that is
    a bug worth surfacing loudly.
    """

    _DISABLED_MSG = (
        "PolymarketExecutor is disabled: Polymarket trading is geoblocked for "
        "US users. Signals targeting Polymarket are logged as "
        "signal.polymarket_data_only and skipped in OrderManager.place()."
    )

    def __init__(self, dry_run: bool = True) -> None:
        # Accept dry_run for signature compatibility but do not use it.
        self._dry_run = dry_run

    async def submit(self, order: Order) -> None:
        raise RuntimeError(self._DISABLED_MSG)

    async def cancel(self, order: Order) -> None:
        raise RuntimeError(self._DISABLED_MSG)

    async def refresh(self, order: Order) -> None:
        raise RuntimeError(self._DISABLED_MSG)


# ── Paper executor (venue-agnostic — Fork 4) ──────────────────────────────────

class PaperExecutor:
    """Venue-agnostic paper executor (build step 2; Fork 4 = single
    PaperExecutor, no per-venue protocol).

    Submits NOTHING.  :meth:`submit` flips PENDING -> PLACED so the
    order-lifecycle consumers see consistent state, and :meth:`refresh` is
    a no-op because the paper :class:`fill_simulator.FillSimulator`
    (main.py) owns the FILLED transition -- exactly the contract
    ``KalshiExecutor`` honours under ``dry_run_paper_mode=True``.

    Used for venues with no live executor when running in paper mode --
    today only Polymarket (live trading geoblocked / data-only).  Kalshi
    keeps using ``KalshiExecutor``'s dry-run path; routing it here too would
    needlessly disturb the existing optimistic-fill regression suite.
    """

    async def submit(self, order: Order) -> None:
        logger.info(
            "paper.dry_run_submit",
            platform=order.platform,
            contract=order.contract_id,
            side=order.side,
            limit_price=round(order.limit_price, 4),
            size_usd=order.size_usd,
        )
        order.transition(
            OrderState.PLACED, platform_order_id="paper-" + order.client_order_id,
        )

    async def cancel(self, order: Order) -> None:
        order.transition(OrderState.CANCELLED)

    async def refresh(self, order: Order) -> None:
        # The FillSimulator owns the FILLED transition; refresh is a no-op
        # so the executor never double-fills an order the simulator has
        # already evaluated (mirrors KalshiExecutor paper-mode refresh).
        return

    async def aclose(self) -> None:
        return


# ── Order manager ──────────────────────────────────────────────────────────────

class OrderManager:
    """Owns all in-flight orders and routes to the correct backend executor.

    Usage::

        mgr = OrderManager(dry_run=True)
        order = await mgr.place(signal, size_usd=200.0)
        await mgr.refresh_all()

    The ``dry_run_paper_mode`` flag is forwarded to the underlying
    :class:`KalshiExecutor`.  When True (only meaningful with
    ``dry_run=True``), ``refresh()`` becomes a no-op so the Round 8 paper
    fill simulator owns the FILLED transition.  Default is False to
    preserve existing test behaviour.
    """

    def __init__(
        self, dry_run: bool = True, dry_run_paper_mode: bool = False
    ) -> None:
        self._dry_run = dry_run
        self._dry_run_paper_mode = dry_run_paper_mode
        self._kalshi = KalshiExecutor(
            dry_run=dry_run, dry_run_paper_mode=dry_run_paper_mode
        )
        # Venue-agnostic paper executor (build step 2).  Only used to route
        # Polymarket orders in paper mode; Kalshi keeps using _kalshi.
        self._paper = PaperExecutor()
        self._orders: dict[str, Order] = {}          # client_order_id → Order
        self._seen_signals: set[str] = set()         # deduplication by signal fingerprint

    def _executor(self, platform: DataSource) -> OrderExecutor:
        if platform == DataSource.KALSHI:
            return self._kalshi  # type: ignore[return-value]
        # Build step 2: in PAPER mode, Polymarket routes to the
        # venue-agnostic PaperExecutor and into the shared FillSimulator.
        # In LIVE mode PM is intercepted in place() before reaching here
        # (geoblocked / data-only), so this branch is paper-only.
        if platform == DataSource.POLYMARKET and self._dry_run_paper_mode:
            return self._paper  # type: ignore[return-value]
        # Any other platform — or PM in live mode (should be unreachable;
        # place() drops it first) — is unsupported.
        raise RuntimeError(
            f"No executor for platform {platform!r} — Kalshi is the only "
            f"live execution venue (Polymarket is paper-only)."
        )

    def _signal_fingerprint(self, signal: ArbitrageSignal) -> str:
        """Stable ID for a signal — prevents duplicate orders on repeated scans."""
        return (
            f"{signal.pm_quote.contract_id}:"
            f"{signal.trade_side}:"
            f"{signal.pm_quote.expiry.isoformat()}"
        )

    def is_duplicate(self, signal: ArbitrageSignal) -> bool:
        """Read-only: would :meth:`place` dedupe this signal right now?

        Mutates nothing -- the fingerprint is only registered inside
        place().  Lets the risk-limit layer (risk-limit goal) skip cap
        evaluation for signals place() would drop anyway, so an
        already-placed signal re-arriving on a later scan cannot emit
        spurious risk_block records.
        """
        return self._signal_fingerprint(signal) in self._seen_signals

    async def place(self, signal: ArbitrageSignal, size_usd: float) -> Order | None:
        """Create and submit an order for a signal.

        Returns None if the signal is deduplicated, or (LIVE mode only) if
        it targets Polymarket -- geoblocked for US users and therefore
        data-only.  In PAPER mode (build step 2) Polymarket is routed to the
        shared paper FillSimulator instead of being dropped.
        """
        fp = self._signal_fingerprint(signal)
        if fp in self._seen_signals:
            logger.debug("order.deduplicated", fingerprint=fp)
            return None
        self._seen_signals.add(fp)

        platform = signal.pm_quote.source
        side = "yes" if signal.trade_side == "buy_yes" else "no"

        # Polymarket has no live execution (US-restricted).  In LIVE mode,
        # log and skip -- the live-trading guardrail is untouched.  In PAPER
        # mode (dry_run_paper_mode=True) the drop is lifted (build step 2):
        # PM falls through to the PaperExecutor + shared FillSimulator,
        # having already cleared the same 12 gates Kalshi does.
        if platform == DataSource.POLYMARKET and not self._dry_run_paper_mode:
            logger.info(
                "signal.polymarket_data_only",
                contract=signal.pm_quote.contract_id,
                side=side,
                trade_side=signal.trade_side,
                raw_edge=round(signal.raw_edge, 4),
                adjusted_edge=round(signal.adjusted_edge, 4),
                fingerprint=fp,
            )
            return None

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
        await self._paper.aclose()
