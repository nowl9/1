"""Shared state manager — single source of truth for the dashboard.

``AgentState`` holds all mutable data.  ``SharedState`` wraps it with an
``asyncio.Lock`` so FastAPI handlers and agent tasks share it safely in one
event loop.

``snapshot()`` produces the canonical JSON payload broadcast over WebSocket.
The schema is defined here so the frontend and tests both reference one place.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── State dataclass ────────────────────────────────────────────────────────────

@dataclass
class FeedStatus:
    status: str = "unknown"       # "ok" | "stale" | "disconnected" | "unknown"
    latency_ms: int = 0
    last_tick: float = 0.0        # Unix timestamp of last received tick
    is_stale: bool = True


@dataclass
class AgentState:
    """All mutable agent state, updated by background tasks."""

    # ── Control ───────────────────────────────────────────────────────────────
    paused: bool = False
    dry_run: bool = True
    started_at: float = field(default_factory=time.time)
    live_mode_token: str = ""

    # ── Market data ───────────────────────────────────────────────────────────
    btc_price: float | None = None

    # feed name → FeedStatus (keyed "deribit", "polymarket", "kalshi")
    feeds: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── Vol surface ───────────────────────────────────────────────────────────
    vol_surface: dict[str, Any] = field(default_factory=dict)
    # keys: svi_rmse, active_smiles, rho, last_fit_timestamp

    # ── Volatility regime ─────────────────────────────────────────────────────
    volatility_regime: dict[str, Any] = field(default_factory=dict)
    # keys: current, rv_1h, rv_24h, effective_min_edge

    # ── Signals ───────────────────────────────────────────────────────────────
    signals: list[dict[str, Any]] = field(default_factory=list)

    # ── Positions ─────────────────────────────────────────────────────────────
    positions: list[dict[str, Any]] = field(default_factory=list)
    positions_summary: dict[str, Any] = field(default_factory=dict)

    # ── Settlements ───────────────────────────────────────────────────────────
    settlements: list[dict[str, Any]] = field(default_factory=list)

    # ── Performance ───────────────────────────────────────────────────────────
    performance: dict[str, Any] = field(default_factory=dict)

    # ── Risk config (live mirror of RiskManager config) ───────────────────────
    risk_config: dict[str, Any] = field(default_factory=dict)

    # ── Round 8 Commit 3: Paper-trading panels ────────────────────────────────
    # Open paper positions in dashboard PositionRow schema.  Populated by
    # main.Agent._paper_position_payload from PaperPositionTracker.open_positions().
    paper_open_positions: list[dict[str, Any]] = field(default_factory=list)
    # Closed paper positions in dashboard settle-row schema.  Populated by
    # main.Agent._paper_settlement_payload from PaperPositionTracker.closed_positions().
    paper_settlements: list[dict[str, Any]] = field(default_factory=list)
    # Aggregate stats from PaperPositionTracker.performance_summary().
    paper_performance: dict[str, Any] = field(default_factory=dict)
    # Signal funnel counters (observed → passed → placed → filled/no_fill).
    # Populated by main.Agent._funnel.
    signal_fired_skipped: dict[str, Any] = field(default_factory=dict)


# ── SharedState ────────────────────────────────────────────────────────────────

class SharedState:
    """asyncio-safe wrapper around AgentState.

    Usage::

        state = SharedState(dry_run=True, live_mode_token="abc")

        async with state.write() as s:
            s.btc_price = 62_000.0

        snap = await state.snapshot()   # → JSON-serializable dict
    """

    def __init__(self, dry_run: bool = True, live_mode_token: str = "") -> None:
        self._state = AgentState(dry_run=dry_run, live_mode_token=live_mode_token)
        self._lock = asyncio.Lock()

    # ── Context managers ──────────────────────────────────────────────────────

    class _Ctx:
        def __init__(self, shared: "SharedState") -> None:
            self._shared = shared

        async def __aenter__(self) -> AgentState:
            await self._shared._lock.acquire()
            return self._shared._state

        async def __aexit__(self, *_: object) -> None:
            self._shared._lock.release()

    def write(self) -> "_Ctx":
        return self._Ctx(self)

    def read(self) -> "_Ctx":
        return self._Ctx(self)

    # ── Snapshot ──────────────────────────────────────────────────────────────

    async def snapshot(self) -> dict[str, Any]:
        """Return the full JSON-serializable dashboard payload."""
        async with self.read() as s:
            uptime = time.time() - s.started_at

            if s.paused:
                agent_status = "paused"
            elif s.dry_run:
                agent_status = "dry_run"
            else:
                agent_status = "running"

            return {
                "timestamp": time.time(),
                "btc_price": s.btc_price,
                "agent_status": agent_status,
                "uptime_seconds": round(uptime, 1),

                "feeds": s.feeds,
                "vol_surface": s.vol_surface,
                "volatility_regime": s.volatility_regime,

                "signals": s.signals,

                "positions": s.positions,
                "positions_summary": s.positions_summary,

                "settlements": s.settlements,
                "performance": s.performance,

                "risk_config": s.risk_config,

                "paper_open_positions": s.paper_open_positions,
                "paper_settlements": s.paper_settlements,
                "paper_performance": s.paper_performance,
                "signal_fired_skipped": s.signal_fired_skipped,
            }
