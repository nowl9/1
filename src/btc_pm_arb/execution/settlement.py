"""Settlement monitor — tracks expiry times and records contract outcomes.

Responsibilities
----------------
1. Alert loop: emits structured log events at T-1h, T-15m, T-5m before expiry.
2. Settlement recording: when a contract resolves, records the outcome
   (win / loss / push) and updates the strategy performance ledger.
3. Performance analytics: win rate, average edge captured vs theoretical,
   P&L by contract type (Kalshi vs Polymarket).

Design
------
* ``SettlementMonitor`` owns an asyncio background task that wakes up
  at the nearest upcoming alert boundary.
* It does *not* query any external API for settlement prices; those are
  pushed in by the orchestrator (``record_settlement``).
* The ledger is an in-memory list of ``SettlementRecord`` objects.
  Persistence (database / file) is out of scope for this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

import structlog

from btc_pm_arb.execution.positions import PositionTracker
from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# Alert thresholds before expiry
_ALERT_OFFSETS: list[timedelta] = [
    timedelta(hours=1),
    timedelta(minutes=15),
    timedelta(minutes=5),
]


# ── Record types ──────────────────────────────────────────────────────────────

class Outcome(str, Enum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"


@dataclass
class TrackedContract:
    """A contract we are watching for settlement."""

    contract_id: str
    platform: DataSource
    expiry: datetime
    theoretical_edge: float     # edge at time of entry
    side: str                   # "yes" or "no"
    entry_price: float          # fill price
    size_usd: float             # notional at entry

    alerted_at: set[str] = field(default_factory=set)   # alert labels already fired


@dataclass
class SettlementRecord:
    """Immutable record of a settled contract."""

    contract_id: str
    platform: DataSource
    expiry: datetime
    side: str
    entry_price: float
    settlement_price: float     # 1.0 = YES resolved, 0.0 = NO resolved
    size_usd: float
    theoretical_edge: float     # edge at time of signal
    realized_pnl: float         # actual P&L after settlement
    fees_usd: float
    outcome: Outcome
    settled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def edge_captured(self) -> float:
        """Fraction of theoretical edge actually realised."""
        if abs(self.theoretical_edge) < 1e-9:
            return 0.0
        payout = self.settlement_price if self.side == "yes" else (1.0 - self.settlement_price)
        actual_edge = payout - self.entry_price
        return actual_edge / self.theoretical_edge


# ── Monitor ───────────────────────────────────────────────────────────────────

class SettlementMonitor:
    """Track upcoming expiries and record settlement outcomes.

    Usage::

        monitor = SettlementMonitor(tracker)
        monitor.track(contract_id, platform, expiry, edge=0.08, side="yes",
                      entry_price=0.42, size_usd=200.0)

        # In background:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(monitor.run(stop_event))

        # When settlement is known:
        monitor.record_settlement(contract_id, platform, settlement_price=1.0)
    """

    def __init__(self, tracker: PositionTracker) -> None:
        self._tracker = tracker
        self._contracts: dict[str, TrackedContract] = {}   # contract_id → contract
        self._ledger: list[SettlementRecord] = []

    # ── Tracking ──────────────────────────────────────────────────────────────

    def track(
        self,
        contract_id: str,
        platform: DataSource,
        expiry: datetime,
        theoretical_edge: float,
        side: str,
        entry_price: float,
        size_usd: float,
    ) -> None:
        """Register a contract for expiry monitoring."""
        if contract_id in self._contracts:
            return  # already watching
        self._contracts[contract_id] = TrackedContract(
            contract_id=contract_id,
            platform=platform,
            expiry=expiry,
            theoretical_edge=theoretical_edge,
            side=side,
            entry_price=entry_price,
            size_usd=size_usd,
        )
        logger.info(
            "settlement.tracking",
            contract=contract_id,
            platform=platform,
            expiry=expiry.isoformat(),
            edge=round(theoretical_edge, 4),
            side=side,
        )

    def record_settlement(
        self,
        contract_id: str,
        platform: DataSource,
        settlement_price: float,
    ) -> SettlementRecord | None:
        """Record the final settlement outcome for a contract.

        Args:
            settlement_price: 1.0 if YES resolved True, 0.0 if YES resolved False.
        """
        tc = self._contracts.get(contract_id)
        if tc is None:
            logger.warning("settlement.unknown_contract", contract=contract_id)
            return None

        # Resolve the matching positions to get realized P&L and fees
        settled = self._tracker.settle(contract_id, settlement_price, platform)
        realized_pnl = sum(p.realized_pnl for p in settled)
        fees_usd = sum(p.fees_usd for p in settled)

        payout_price = settlement_price if tc.side == "yes" else (1.0 - settlement_price)
        if payout_price > tc.entry_price + 1e-4:
            outcome = Outcome.WIN
        elif payout_price < tc.entry_price - 1e-4:
            outcome = Outcome.LOSS
        else:
            outcome = Outcome.PUSH

        record = SettlementRecord(
            contract_id=contract_id,
            platform=platform,
            expiry=tc.expiry,
            side=tc.side,
            entry_price=tc.entry_price,
            settlement_price=settlement_price,
            size_usd=tc.size_usd,
            theoretical_edge=tc.theoretical_edge,
            realized_pnl=realized_pnl,
            fees_usd=fees_usd,
            outcome=outcome,
        )
        self._ledger.append(record)

        logger.info(
            "settlement.recorded",
            contract=contract_id,
            outcome=outcome,
            settlement_price=settlement_price,
            entry_price=tc.entry_price,
            realized_pnl=round(realized_pnl, 4),
            edge_captured=round(record.edge_captured, 4),
        )

        del self._contracts[contract_id]
        return record

    # ── Alert loop ────────────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        """Background task: emit alerts as expiries approach."""
        logger.info("settlement_monitor.started")
        while not stop_event.is_set():
            now = datetime.now(timezone.utc)
            self._fire_alerts(now)
            sleep_secs = self._secs_to_next_check()
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()),
                    timeout=sleep_secs,
                )
            except asyncio.TimeoutError:
                pass  # Normal — loop and check again
        logger.info("settlement_monitor.stopped")

    def _fire_alerts(self, now: datetime) -> None:
        for tc in list(self._contracts.values()):
            for offset in _ALERT_OFFSETS:
                label = _offset_label(offset)
                if label in tc.alerted_at:
                    continue
                alert_time = tc.expiry - offset
                if now >= alert_time:
                    tc.alerted_at.add(label)
                    logger.warning(
                        "settlement.expiry_alert",
                        contract=tc.contract_id,
                        platform=tc.platform,
                        expiry=tc.expiry.isoformat(),
                        alert=label,
                        minutes_remaining=round((tc.expiry - now).total_seconds() / 60, 1),
                    )

    def _secs_to_next_check(self) -> float:
        """Sleep until the nearest unfired alert boundary, min 10 s, max 60 s."""
        if not self._contracts:
            return 60.0
        now = datetime.now(timezone.utc)
        upcoming: list[float] = []
        for tc in self._contracts.values():
            for offset in _ALERT_OFFSETS:
                label = _offset_label(offset)
                if label not in tc.alerted_at:
                    secs = (tc.expiry - offset - now).total_seconds()
                    if secs > 0:
                        upcoming.append(secs)
        if not upcoming:
            return 60.0
        return max(10.0, min(60.0, min(upcoming)))

    # ── Analytics ─────────────────────────────────────────────────────────────

    def performance_summary(self) -> dict:
        """Aggregate settlement statistics."""
        ledger = self._ledger
        if not ledger:
            return {"total_settled": 0}

        wins = [r for r in ledger if r.outcome == Outcome.WIN]
        losses = [r for r in ledger if r.outcome == Outcome.LOSS]
        pushes = [r for r in ledger if r.outcome == Outcome.PUSH]

        by_platform: dict[str, dict] = {}
        for r in ledger:
            bucket = by_platform.setdefault(str(r.platform), {"pnl": 0.0, "count": 0})
            bucket["pnl"] += r.realized_pnl
            bucket["count"] += 1

        avg_edge_captured = (
            sum(r.edge_captured for r in ledger) / len(ledger)
            if ledger else 0.0
        )

        return {
            "total_settled": len(ledger),
            "wins": len(wins),
            "losses": len(losses),
            "pushes": len(pushes),
            "win_rate": len(wins) / len(ledger),
            "total_realized_pnl": round(sum(r.realized_pnl for r in ledger), 4),
            "total_fees_usd": round(sum(r.fees_usd for r in ledger), 4),
            "avg_edge_captured": round(avg_edge_captured, 4),
            "avg_theoretical_edge": round(
                sum(r.theoretical_edge for r in ledger) / len(ledger), 4
            ),
            "by_platform": by_platform,
        }

    @property
    def ledger(self) -> list[SettlementRecord]:
        return list(self._ledger)

    @property
    def tracked_contracts(self) -> dict[str, TrackedContract]:
        return dict(self._contracts)


def _offset_label(offset: timedelta) -> str:
    total_secs = int(offset.total_seconds())
    if total_secs >= 3600:
        return f"T-{total_secs // 3600}h"
    return f"T-{total_secs // 60}m"
