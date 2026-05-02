"""Paper-position tracker — in-memory aggregate of the paper-trading event log.

Round 8: maintains live state derived from :class:`PaperOrderRecord` /
:class:`PaperFillRecord` / :class:`PaperSettlementRecord` events, plus the
mark-to-market loop that updates ``current_mid`` from the live PM tick
stream.  The on-disk JSONL files (see :mod:`paper_ledger`) are the ground
truth; this tracker is a derived view that gets reconstructed on agent
restart via :meth:`PaperPositionTracker.replay_from_disk`.

Position keying — ``(platform, contract_id, side)``
---------------------------------------------------
Unlike the existing :class:`PositionTracker` (which keys by
``(platform, contract_id)`` and overwrites side on subsequent fills),
the paper tracker keys positions by the full triple including side.
Going long YES and long NO on the same contract is a hedged structure
that Round 9 calibration may want to study; collapsing them by contract
alone would lose that signal.

Mark-to-market freshness contract
---------------------------------
``last_mark_at`` updates whenever a fresh tick is observed for the
position's contract on a given scan, NOT only when ``current_mid``
actually changes value.  A contract that ticks with a flat mid does
not look stale on the dashboard.  Per the Round 8 commit-1
clarification: "the intent is freshness-of-observation, not
freshness-of-price-change."

This means even one-sided ticks (where the relevant ``side_mid`` is
None) bump ``last_mark_at`` — observing the tick is the freshness
signal, not having a usable mid for the position's side.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

import structlog

from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.models import DataSource, PredictionMarketTick

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── PaperPosition ─────────────────────────────────────────────────────────────


@dataclass
class PaperPosition:
    """Live paper-trading position state for one (platform, contract, side) triple."""

    platform: DataSource
    contract_id: str
    side: Literal["yes", "no"]
    expiry: datetime

    # ── Aggregated from fills ────────────────────────────────────────────────
    filled_size_usd: float = 0.0
    entry_price: float = 0.0          # weighted-average fill price [0, 1]
    fees_usd: float = 0.0
    order_ids: list[str] = field(default_factory=list)

    # ── Mark-to-market ───────────────────────────────────────────────────────
    current_mid: float | None = None
    # Updated on every tick observation for this contract — see module
    # docstring's freshness-of-observation contract.
    last_mark_at: datetime | None = None

    # ── Terminal state ───────────────────────────────────────────────────────
    closed: bool = False
    settlement_price: float | None = None
    realized_pnl: float = 0.0

    # ── Audit ────────────────────────────────────────────────────────────────
    opened_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def unrealized_pnl(self) -> float:
        if (
            self.current_mid is None
            or self.filled_size_usd == 0
            or self.closed
        ):
            return 0.0
        return (self.current_mid - self.entry_price) * self.filled_size_usd

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl - self.fees_usd

    def snapshot(self) -> dict[str, Any]:
        """Render as a JSON-friendly dict for the dashboard payload."""
        return {
            "platform": self.platform.value,
            "contract_id": self.contract_id,
            "side": self.side,
            "expiry": self.expiry.isoformat(),
            "filled_size_usd": round(self.filled_size_usd, 4),
            "entry_price": round(self.entry_price, 4),
            "current_mid": (
                round(self.current_mid, 4) if self.current_mid is not None else None
            ),
            "last_mark_at": (
                self.last_mark_at.isoformat() if self.last_mark_at is not None else None
            ),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "fees_usd": round(self.fees_usd, 4),
            "total_pnl": round(self.total_pnl, 4),
            "closed": self.closed,
            "settlement_price": (
                round(self.settlement_price, 4)
                if self.settlement_price is not None
                else None
            ),
            "opened_at": self.opened_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "order_ids": list(self.order_ids),
        }


# ── PaperPositionTracker ──────────────────────────────────────────────────────

PositionKey = tuple[DataSource, str, str]   # (platform, contract_id, side)


class PaperPositionTracker:
    """Maintain :class:`PaperPosition` state from event records.

    Usage::

        tracker = PaperPositionTracker()
        tracker.record_fill(order_record=order, fill_record=fill)
        tracker.mark_to_market(latest_pm_ticks)        # every scan tick
        tracker.settle(settlement_record)              # on Kalshi resolve

        # On agent restart:
        tracker.replay_from_disk(ledger)
    """

    def __init__(self) -> None:
        self._positions: dict[PositionKey, PaperPosition] = {}

    # ── Event handlers ────────────────────────────────────────────────────────

    def record_fill(
        self,
        *,
        order_record: PaperOrderRecord,
        fill_record: PaperFillRecord,
    ) -> PaperPosition | None:
        """Update (or create) a position from a fill event.

        Returns the affected position, or ``None`` for ``no_fill``
        outcomes (no position state change).  Idempotent on a second
        identical call only insofar as the same fill record will compound
        weighted-average entry — the caller is responsible for not
        replaying the same (order, fill) pair twice; the replay path in
        :meth:`replay_from_disk` reads each line once.
        """
        if fill_record.fill_outcome == "no_fill":
            return None
        if fill_record.fill_price is None or fill_record.fill_size_usd <= 0:
            logger.warning(
                "paper_position.invalid_fill",
                client_id=fill_record.client_order_id,
                outcome=fill_record.fill_outcome,
                fill_price=fill_record.fill_price,
                fill_size_usd=fill_record.fill_size_usd,
            )
            return None

        key: PositionKey = (
            order_record.platform,
            order_record.contract_id,
            order_record.side,
        )
        pos = self._positions.get(key)
        if pos is None:
            pos = PaperPosition(
                platform=order_record.platform,
                contract_id=order_record.contract_id,
                side=order_record.side,
                expiry=order_record.expiry,
                opened_at=fill_record.filled_at,
                updated_at=fill_record.filled_at,
            )
            self._positions[key] = pos

        # Weighted-average entry across all fills on this triple
        total_before = pos.filled_size_usd
        new_size = fill_record.fill_size_usd
        if total_before + new_size > 0:
            pos.entry_price = (
                pos.entry_price * total_before
                + fill_record.fill_price * new_size
            ) / (total_before + new_size)
        pos.filled_size_usd += new_size
        pos.fees_usd += fill_record.fees_usd
        if order_record.client_order_id not in pos.order_ids:
            pos.order_ids.append(order_record.client_order_id)
        pos.updated_at = fill_record.filled_at
        return pos

    def settle(self, settlement_record: PaperSettlementRecord) -> PaperPosition | None:
        """Mark a position closed with realized P&L from the settlement record."""
        key: PositionKey = (
            settlement_record.platform,
            settlement_record.contract_id,
            settlement_record.side,
        )
        pos = self._positions.get(key)
        if pos is None:
            logger.warning(
                "paper_position.settlement_without_position",
                client_id=settlement_record.client_order_id,
                contract=settlement_record.contract_id,
                platform=settlement_record.platform.value,
                side=settlement_record.side,
            )
            return None
        pos.realized_pnl = settlement_record.realized_pnl
        pos.fees_usd = settlement_record.fees_usd
        pos.settlement_price = settlement_record.settlement_price
        pos.current_mid = settlement_record.settlement_price
        pos.closed = True
        pos.updated_at = settlement_record.settled_at
        return pos

    # ── Mark-to-market ────────────────────────────────────────────────────────

    def mark_to_market(self, pm_ticks: Iterable[PredictionMarketTick]) -> None:
        """Update ``current_mid`` and ``last_mark_at`` for open positions.

        Builds a per-(source, contract) latest-tick index from the input
        (last-write-wins on ties; we tolerate timestamps equal because
        feed cadence is coarser than wall-clock resolution), then for each
        open position, applies the side-appropriate mid.

        Per the freshness-of-observation contract: ``last_mark_at`` is
        bumped on every observation, even when the side-appropriate mid
        is None (one-sided book) — the dashboard wants to know the
        contract is ticking, not just that its price has moved.
        """
        latest: dict[tuple[DataSource, str], PredictionMarketTick] = {}
        for t in pm_ticks:
            key2 = (t.source, t.contract_id)
            prev = latest.get(key2)
            if prev is None or t.timestamp >= prev.timestamp:
                latest[key2] = t

        for pos in self._positions.values():
            if pos.closed:
                continue
            tick = latest.get((pos.platform, pos.contract_id))
            if tick is None:
                continue
            # Side-aware mid: YES position uses yes_mid, NO position uses no_mid.
            side_mid = tick.yes_mid if pos.side == "yes" else tick.no_mid
            # Freshness of observation, not freshness of price change —
            # bump even when side_mid is None.
            pos.last_mark_at = tick.timestamp
            if side_mid is not None:
                pos.current_mid = side_mid
            pos.updated_at = tick.timestamp

    # ── Replay ────────────────────────────────────────────────────────────────

    def replay_from_disk(self, ledger: PaperLedger) -> None:
        """Reconstruct in-memory state from the JSONL event streams.

        Ordering: orders are buffered first (so fills can resolve their
        ``client_order_id`` references), then fills are applied in file
        order, then settlements.  This matches the chronological event
        order — orders precede their fills (same scan tick), fills
        precede settlements (different scans).
        """
        orders_by_id: dict[str, PaperOrderRecord] = {}
        for order in ledger.replay_orders():
            orders_by_id[order.client_order_id] = order

        for fill in ledger.replay_fills():
            order = orders_by_id.get(fill.client_order_id)
            if order is None:
                logger.warning(
                    "paper_position.fill_without_order",
                    client_id=fill.client_order_id,
                )
                continue
            self.record_fill(order_record=order, fill_record=fill)

        for settlement in ledger.replay_settlements():
            self.settle(settlement)

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(
        self,
        platform: DataSource,
        contract_id: str,
        side: Literal["yes", "no"],
    ) -> PaperPosition | None:
        return self._positions.get((platform, contract_id, side))

    def all_positions(self) -> list[PaperPosition]:
        return list(self._positions.values())

    def open_positions(self) -> list[PaperPosition]:
        return [p for p in self._positions.values() if not p.closed]

    def closed_positions(self) -> list[PaperPosition]:
        return [p for p in self._positions.values() if p.closed]

    def total_exposure_usd(self) -> float:
        return sum(p.filled_size_usd for p in self.open_positions())

    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.open_positions())

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())

    def total_fees_usd(self) -> float:
        return sum(p.fees_usd for p in self._positions.values())

    def performance_summary(self) -> dict[str, Any]:
        open_p = self.open_positions()
        closed_p = self.closed_positions()
        wins = [p for p in closed_p if p.realized_pnl > 0]
        losses = [p for p in closed_p if p.realized_pnl < 0]
        pushes = [p for p in closed_p if p.realized_pnl == 0]

        realized = self.total_realized_pnl()
        unrealized = self.total_unrealized_pnl()
        fees = self.total_fees_usd()

        return {
            "total_paper_positions": len(self._positions),
            "open_paper_positions": len(open_p),
            "closed_paper_positions": len(closed_p),
            "win_count": len(wins),
            "loss_count": len(losses),
            "push_count": len(pushes),
            "win_rate": (len(wins) / len(closed_p)) if closed_p else 0.0,
            "total_realized_pnl": round(realized, 4),
            "total_unrealized_pnl": round(unrealized, 4),
            "total_paper_pnl": round(realized + unrealized - fees, 4),
            "total_fees_usd": round(fees, 4),
            "total_exposure_usd": round(self.total_exposure_usd(), 4),
        }

    def snapshot_state(self) -> dict[str, Any]:
        """Return a deterministic, comparable representation for tests/replay.

        Sorted by ``(platform, contract_id, side)`` so two trackers built
        from the same events compare equal regardless of dict insertion
        order.  Datetimes compare by value; enums compare by identity.
        """
        def _sort_key(p: PaperPosition) -> tuple[str, str, str]:
            return (p.platform.value, p.contract_id, p.side)

        open_sorted = sorted(self.open_positions(), key=_sort_key)
        closed_sorted = sorted(self.closed_positions(), key=_sort_key)
        return {
            "open": [asdict(p) for p in open_sorted],
            "closed": [asdict(p) for p in closed_sorted],
            "performance": self.performance_summary(),
        }
