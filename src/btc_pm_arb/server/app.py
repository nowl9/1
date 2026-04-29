"""FastAPI dashboard backend.

Endpoints
---------
GET  /api/health          liveness probe
GET  /api/status          agent status, uptime, BTC price
GET  /api/config          current risk config (read-only, no auth)
WS   /ws/snapshot         push full snapshot every 1.5 s
POST /api/pause           pause execution
POST /api/resume          resume execution
POST /api/risk-config     update risk limits
POST /api/mode            toggle dry-run / live (requires confirmation token)

Static
------
GET  /                    serves server/static/index.html (frontend SPA)

Auth
----
Control endpoints (POST) require ``Authorization: Bearer <DASHBOARD_TOKEN>``.
Read-only endpoints and WebSocket need no auth.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from btc_pm_arb.server.state import SharedState

logger: structlog.BoundLogger = structlog.get_logger(__name__)

_SNAPSHOT_INTERVAL: float = 1.5
_INSECURE_TOKEN = "dev-token-change-me"
_STATIC_DIR = Path(__file__).parent / "static"


# ── Request models ────────────────────────────────────────────────────────────

class RiskConfigRequest(BaseModel):
    max_position_per_contract_usd: float | None = None
    max_total_exposure_usd: float | None = None
    max_open_positions: int | None = None
    max_correlated_exposure_usd: float | None = None
    correlated_strike_band_pct: float | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    min_edge: float | None = Field(default=None, ge=0.0, le=1.0)


class ModeRequest(BaseModel):
    live: bool
    confirmation_token: str


# ── App factory ────────────────────────────────────────────────────────────────

def create_app(shared_state: SharedState | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="BTC PM Arb Dashboard", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    state = shared_state or SharedState(dry_run=True)
    token = os.environ.get("DASHBOARD_TOKEN", _INSECURE_TOKEN)
    if token == _INSECURE_TOKEN:
        logger.warning("dashboard.insecure_token_in_use")

    bearer = HTTPBearer()

    # ── Auth dependency ───────────────────────────────────────────────────────

    async def require_auth(
        creds: HTTPAuthorizationCredentials = Depends(bearer),
    ) -> None:
        if creds.credentials != token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
            )

    # ── GET /api/health ───────────────────────────────────────────────────────

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "timestamp": time.time()}

    # ── GET /api/status ───────────────────────────────────────────────────────

    @app.get("/api/status")
    async def get_status() -> JSONResponse:
        snap = await state.snapshot()
        return JSONResponse({
            "agent_status": snap["agent_status"],
            "uptime_seconds": snap["uptime_seconds"],
            "btc_price": snap["btc_price"],
            "volatility_regime": snap.get("volatility_regime", {}),
            "timestamp": snap["timestamp"],
        })

    # ── GET /api/config ───────────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        """Current risk config — no auth required (read-only)."""
        async with state.read() as s:
            return JSONResponse(s.risk_config)

    # ── WebSocket /ws/snapshot ────────────────────────────────────────────────

    @app.websocket("/ws/snapshot")
    async def ws_snapshot(ws: WebSocket) -> None:
        await ws.accept()
        logger.info("dashboard.ws_connected", client=str(ws.client))
        try:
            while True:
                snap = await state.snapshot()
                await ws.send_text(json.dumps(snap))
                await asyncio.sleep(_SNAPSHOT_INTERVAL)
        except WebSocketDisconnect:
            logger.info("dashboard.ws_disconnected", client=str(ws.client))
        except Exception as exc:
            logger.warning("dashboard.ws_error", error=str(exc))

    # ── POST /api/pause ───────────────────────────────────────────────────────

    @app.post("/api/pause", dependencies=[Depends(require_auth)])
    async def pause() -> dict[str, str]:
        async with state.write() as s:
            s.paused = True
        logger.info("dashboard.agent_paused")
        return {"status": "paused"}

    # ── POST /api/resume ──────────────────────────────────────────────────────

    @app.post("/api/resume", dependencies=[Depends(require_auth)])
    async def resume() -> dict[str, str]:
        async with state.write() as s:
            s.paused = False
        logger.info("dashboard.agent_resumed")
        return {"status": "running"}

    # ── POST /api/risk-config ─────────────────────────────────────────────────

    @app.post("/api/risk-config", dependencies=[Depends(require_auth)])
    async def update_risk_config(req: RiskConfigRequest) -> dict[str, Any]:
        async with state.write() as s:
            cfg = s.risk_config
            updates = req.model_dump(exclude_none=True)
            cfg.update(updates)
            updated = dict(cfg)
        logger.info("dashboard.risk_config_updated", **updated)
        return {"status": "ok", "risk_config": updated}

    # ── POST /api/mode ────────────────────────────────────────────────────────

    @app.post("/api/mode", dependencies=[Depends(require_auth)])
    async def set_mode(req: ModeRequest) -> dict[str, Any]:
        if req.live:
            async with state.read() as s:
                expected = s.live_mode_token
            if req.confirmation_token != expected:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid confirmation token for live mode",
                )
        async with state.write() as s:
            s.dry_run = not req.live
        mode = "live" if req.live else "dry_run"
        logger.warning("dashboard.mode_changed", mode=mode)
        return {"status": "ok", "mode": mode}

    # ── Static files (frontend SPA) ───────────────────────────────────────────
    # Mounted last so API routes take priority.
    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


# Module-level instance for ``uvicorn btc_pm_arb.server.app:app``
app = create_app()
