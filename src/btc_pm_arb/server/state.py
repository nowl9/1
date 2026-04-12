"""Shared state manager — collects live snapshots from all pipeline layers.

All reads/writes are protected by a single asyncio.Lock so the FastAPI
handlers (which run in the same event loop as the agent) see a consistent
view.

The state dict is designed to be directly JSON-serialisable so the WebSocket
handler can broadcast it without extra serialization steps.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger: structlog.BoundLogger = structlog.get_logger(__name__)


@dataclass
class AgentState:
    """Top-level mutable container for the agent's current state."""

    # Control flags (set by dashboard control endpoints)
    paused: bool = False
    dry_run: bool = True
    started_at: float = field(default_factory=time.time)

    # Latest BTC price (populated by the Deribit feed task)
    btc_price: float | None = None

    # Feed health: source → staleness_ms
    feed_health: dict[str, float] = field(default_factory=dict)

    # Vol surface summary: expiry → {rmse, rho, n_options, forward}
    vol_surface: dict[str, Any] = field(default_factory=dict)

    # Current signals (last scan output)
    signals: list[dict] = field(default_factory=list)

    # Open positions snapshots
    positions: list[dict] = field(default_factory=list)

    # Settled contracts
    settlement_history: list[dict] = field(default_factory=list)

    # Current risk config
    risk_config: dict[str, Any] = field(default_factory=dict)

    # Realized vol and regime
    realized_vol: dict[str, Any] = field(default_factory=dict)   # window_h → rv value
    vol_regime: str = "normal"

    # Live mode confirmation token (set once at startup)
    live_mode_token: str = ""


class SharedState:
    """Thread-safe shared state with asyncio.Lock.

    Usage::

        state = SharedState()

        # Write (from agent tasks):
        async with state.write() as s:
            s.btc_price = 62_000.0

        # Read (from FastAPI handlers):
        async with state.read() as s:
            price = s.btc_price

        # Snapshot (for WebSocket broadcast):
        snap = await state.snapshot()
    """

    def __init__(self, dry_run: bool = True, live_mode_token: str = "") -> None:
        self._state = AgentState(dry_run=dry_run, live_mode_token=live_mode_token)
        self._lock = asyncio.Lock()

    class _WriteCtx:
        def __init__(self, state: "SharedState") -> None:
            self._state = state

        async def __aenter__(self) -> AgentState:
            await self._state._lock.acquire()
            return self._state._state

        async def __aexit__(self, *_: object) -> None:
            self._state._lock.release()

    class _ReadCtx:
        def __init__(self, state: "SharedState") -> None:
            self._state = state

        async def __aenter__(self) -> AgentState:
            await self._state._lock.acquire()
            return self._state._state

        async def __aexit__(self, *_: object) -> None:
            self._state._lock.release()

    def write(self) -> "_WriteCtx":
        return self._WriteCtx(self)

    def read(self) -> "_ReadCtx":
        return self._ReadCtx(self)

    async def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the full state."""
        async with self.read() as s:
            uptime = time.time() - s.started_at
            return {
                "status": "paused" if s.paused else ("dry_run" if s.dry_run else "live"),
                "uptime_s": round(uptime, 1),
                "btc_price": s.btc_price,
                "feed_health": s.feed_health,
                "vol_surface": s.vol_surface,
                "signals": s.signals,
                "positions": s.positions,
                "settlement_history": s.settlement_history,
                "risk_config": s.risk_config,
                "realized_vol": s.realized_vol,
                "vol_regime": s.vol_regime,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
