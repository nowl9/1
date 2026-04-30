"""Position tracker — maintains real-time P&L for every open prediction-market position.

A *position* is opened when an order fills and closed when the contract
settles (or is manually closed via an opposite trade).  This module does not
execute trades — it only records fills reported by the order manager.

P&L accounting
--------------
    unrealized_pnl = (current_mid - entry_price) * filled_size
    realized_pnl   = (exit_price - entry_price) * closed_size  (on settlement / fill)
    fees_usd       = platform fees (Kalshi 0 %, Polymarket gas + spread estimate)

Fee model
---------
* Kalshi: 0 % maker/taker (as of 2024).
* Polymarket: no explicit fee, but CLOB spread is ~1–2 cts; we estimate
  gas cost as a flat $0.01 per order on Polygon.

Positions are keyed by ``(platform, contract_id)``.  Multiple fills on the
same contract are averaged into a single position (FIFO not required given
binary outcome settlement).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from btc_pm_arb.models import DataSource
from btc_pm_arb.execution.orders import Order, OrderState

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# Flat gas estimate for Polymarket (Polygon) per fill
_POLYMARKET_GAS_USD: float = 0.01


# ── Position model ────────────────────────────────────────────────────────────

@dataclass
class Position:
    """Live position for one (platform, contract_id) pair."""

    platform: DataSource
    contract_id: str
    side: str              # "yes" or "no"

    filled_size: float = 0.0          # total notional filled (USD)
    entry_price: float = 0.0          # weighted average fill price [0, 1]
    current_mid: float | None = None  # latest mid-market price [0, 1]

    realized_pnl: float = 0.0
    fees_usd: float = 0.0

    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed: bool = False
    settlement_price: float | None = None   # set on resolution

    # Track contributing orders for audit
    order_ids: list[str] = field(default_factory=list)

    @property
    def unrealized_pnl(self) -> float:
        if self.current_mid is None or self.filled_size == 0:
            return 0.0
        return (self.current_mid - self.entry_price) * self.filled_size

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl - self.fees_usd

    @property
    def notional_usd(self) -> float:
        return self.filled_size

    def snapshot(self) -> dict:
        return {
            "platform": self.platform,
            "contract_id": self.contract_id,
            "side": self.side,
            "filled_size": round(self.filled_size, 4),
            "entry_price": round(self.entry_price, 4),
            "current_mid": round(self.current_mid, 4) if self.current_mid else None,
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "fees_usd": round(self.fees_usd, 4),
            "total_pnl": round(self.total_pnl, 4),
            "closed": self.closed,
            "updated_at": self.updated_at.isoformat(),
        }


# ── Position tracker ──────────────────────────────────────────────────────────

class PositionTracker:
    """Maintain and update positions from order fills.

    Usage::

        tracker = PositionTracker()
        tracker.record_fill(order)          # called by OrderManager after fill
        tracker.update_mid(contract_id, mid_price)
        snapshots = tracker.all_snapshots()
    """

    def __init__(self) -> None:
        # key: (platform, contract_id)
        self._positions: dict[tuple[DataSource, str], Position] = {}

    # ── Recording fills ───────────────────────────────────────────────────────

    def record_fill(self, order: Order) -> Position | None:
        """Update (or create) a position from a filled order.

        Should be called whenever ``order.state == OrderState.FILLED``
        or ``order.state == OrderState.PARTIAL``.

        Returns the updated Position, or None if the order has no fill.
        """
        if order.filled_size <= 0 or order.average_fill_price is None:
            return None

        key = (order.platform, order.contract_id)
        pos = self._positions.get(key)

        if pos is None:
            pos = Position(
                platform=order.platform,
                contract_id=order.contract_id,
                side=order.side,
            )
            self._positions[key] = pos

        if order.client_order_id not in pos.order_ids:
            pos.order_ids.append(order.client_order_id)

        # Weighted-average entry price
        total_before = pos.filled_size
        new_fill = order.filled_size
        if total_before + new_fill > 0:
            pos.entry_price = (
                (pos.entry_price * total_before + order.average_fill_price * new_fill)
                / (total_before + new_fill)
            )
        pos.filled_size += new_fill

        # Fee accounting
        if order.platform == DataSource.KALSHI:
            pos.fees_usd += 0.0  # Kalshi charges 0 % currently
        elif order.platform == DataSource.POLYMARKET:
            pos.fees_usd += _POLYMARKET_GAS_USD

        pos.updated_at = datetime.now(timezone.utc)

        logger.info(
            "position.fill_recorded",
            **pos.snapshot(),
        )
        return pos

    # ── Mid-price updates ─────────────────────────────────────────────────────

    def update_mid(
        self,
        contract_id: str,
        mid_price: float,
        platform: DataSource | None = None,
    ) -> None:
        """Push a new mid-market price into all matching positions."""
        for (plat, cid), pos in self._positions.items():
            if cid == contract_id and (platform is None or plat == platform):
                pos.current_mid = mid_price
                pos.updated_at = datetime.now(timezone.utc)

    # ── Settlement ────────────────────────────────────────────────────────────

    def settle(
        self,
        contract_id: str,
        settlement_price: float,   # 1.0 = YES won, 0.0 = NO won
        platform: DataSource | None = None,
    ) -> list[Position]:
        """Record final settlement for all matching positions."""
        settled: list[Position] = []
        for (plat, cid), pos in self._positions.items():
            if cid != contract_id or pos.closed:
                continue
            if platform is not None and plat != platform:
                continue

            # Realized P&L: YES winner gets 1.0, NO winner gets 1.0 (complement)
            payout_price = settlement_price if pos.side == "yes" else (1.0 - settlement_price)
            pos.realized_pnl += (payout_price - pos.entry_price) * pos.filled_size
            pos.settlement_price = settlement_price
            pos.current_mid = settlement_price
            pos.closed = True
            pos.updated_at = datetime.now(timezone.utc)

            logger.info(
                "position.settled",
                **pos.snapshot(),
                settlement_price=settlement_price,
            )
            settled.append(pos)
        return settled

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(self, contract_id: str, platform: DataSource | None = None) -> list[Position]:
        return [
            pos for (plat, cid), pos in self._positions.items()
            if cid == contract_id and (platform is None or plat == platform)
        ]

    def open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if not p.closed]

    def closed_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.closed]

    def total_exposure_usd(self) -> float:
        return sum(p.notional_usd for p in self.open_positions())

    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.open_positions())

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())

    def all_snapshots(self) -> list[dict]:
        return [p.snapshot() for p in self._positions.values()]

    def performance_summary(self) -> dict:
        closed = self.closed_positions()
        wins = [p for p in closed if p.realized_pnl > 0]
        losses = [p for p in closed if p.realized_pnl <= 0]
        return {
            "total_positions": len(self._positions),
            "open_positions": len(self.open_positions()),
            "closed_positions": len(closed),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "total_realized_pnl": round(self.total_realized_pnl(), 4),
            "total_unrealized_pnl": round(self.total_unrealized_pnl(), 4),
            "total_exposure_usd": round(self.total_exposure_usd(), 4),
        }
