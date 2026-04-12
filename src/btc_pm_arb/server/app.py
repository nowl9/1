"""FastAPI dashboard backend — exposes live agent state and control endpoints.

Endpoints
---------
GET  /api/status          agent status, uptime, BTC price
WS   /ws/snapshot         push full state snapshot every 1.5 s
POST /api/pause           pause signal generation and execution
POST /api/resume          resume signal generation and execution
POST /api/risk-config     update risk limits (validated by RiskConfig)
POST /api/mode            toggle dry-run / live mode (requires confirmation token)

Auth
----
* Control endpoints (POST) require ``Authorization: Bearer <DASHBOARD_TOKEN>``
* The WebSocket and GET /api/status are unauthenticated (local/VPN access)
* Token is read from the ``DASHBOARD_TOKEN`` env variable; defaults to an
  insecure placeholder that logs a warning at startup

Running standalone (for testing)::

    uvicorn btc_pm_arb.server.app:create_app --factory --port 8000
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncGenerator

import structlog
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from btc_pm_arb.execution.risk import RiskConfig
from btc_pm_arb.server.state import SharedState

logger: structlog.BoundLogger = structlog.get_logger(__name__)

_SNAPSHOT_INTERVAL = 1.5   # seconds between WebSocket pushes
_INSECURE_TOKEN = "dev-token-change-me"

# ── Pydantic request models ────────────────────────────────────────────────────

class RiskConfigRequest(BaseModel):
    max_position_per_contract_usd: float | None = None
    max_total_exposure_usd: float | None = None
    max_open_positions: int | None = None
    max_correlated_exposure_usd: float | None = None
    correlated_strike_band_pct: float | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ModeRequest(BaseModel):
    live: bool
    confirmation_token: str   # must match AgentState.live_mode_token


# ── App factory ────────────────────────────────────────────────────────────────

def create_app(shared_state: SharedState | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        shared_state: Shared state object injected from the main orchestrator.
                      If None (standalone mode), a fresh state is created.
    """
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

    # ── GET /api/status ───────────────────────────────────────────────────────

    @app.get("/api/status")
    async def get_status() -> JSONResponse:
        snap = await state.snapshot()
        return JSONResponse({
            "status": snap["status"],
            "uptime_s": snap["uptime_s"],
            "btc_price": snap["btc_price"],
            "vol_regime": snap["vol_regime"],
            "timestamp": snap["timestamp"],
        })

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
            if req.max_position_per_contract_usd is not None:
                cfg["max_position_per_contract_usd"] = req.max_position_per_contract_usd
            if req.max_total_exposure_usd is not None:
                cfg["max_total_exposure_usd"] = req.max_total_exposure_usd
            if req.max_open_positions is not None:
                cfg["max_open_positions"] = req.max_open_positions
            if req.max_correlated_exposure_usd is not None:
                cfg["max_correlated_exposure_usd"] = req.max_correlated_exposure_usd
            if req.correlated_strike_band_pct is not None:
                cfg["correlated_strike_band_pct"] = req.correlated_strike_band_pct
            if req.min_confidence is not None:
                cfg["min_confidence"] = req.min_confidence
            updated = dict(cfg)
        logger.info("dashboard.risk_config_updated", **updated)
        return {"status": "ok", "risk_config": updated}

    # ── POST /api/mode ────────────────────────────────────────────────────────

    @app.post("/api/mode", dependencies=[Depends(require_auth)])
    async def set_mode(req: ModeRequest) -> dict[str, Any]:
        if req.live:
            async with state.read() as s:
                expected_token = s.live_mode_token
            if req.confirmation_token != expected_token:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid confirmation token for live mode",
                )
        async with state.write() as s:
            s.dry_run = not req.live
        mode = "live" if req.live else "dry_run"
        logger.warning("dashboard.mode_changed", mode=mode)
        return {"status": "ok", "mode": mode}

    return app


# ── Uvicorn entrypoint (standalone) ──────────────────────────────────────────

app = create_app()   # module-level instance for ``uvicorn btc_pm_arb.server.app:app``
