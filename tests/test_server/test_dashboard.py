"""Tests for server/app.py and server/state.py — dashboard backend.

Tests cover:
  - GET /api/health returns ok + timestamp
  - GET /api/config returns risk config (no auth)
  - GET /api/status returns current agent state
  - WebSocket /ws/snapshot pushes JSON snapshot with all required keys
  - POST /api/pause and /api/resume toggle paused flag
  - POST /api/mode rejects live mode with wrong token
  - Auth: control endpoints reject missing/wrong bearer token
  - SharedState snapshot serialization with enriched schema
  - SharedState concurrent read/write safety
  - CORS headers on responses
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from btc_pm_arb.server.app import create_app
from btc_pm_arb.server.state import SharedState

_TOKEN = "test-secret-token"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def shared_state() -> SharedState:
    return SharedState(dry_run=True, live_mode_token="live-tok")


@pytest.fixture
def client(shared_state: SharedState, monkeypatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    return TestClient(app)


def _auth(token: str = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── GET /api/health ───────────────────────────────────────────────────────────

def test_health_returns_200(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_health_fields(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "timestamp" in body
    assert isinstance(body["timestamp"], float)


def test_health_no_auth_required(client):
    resp = client.get("/api/health", headers={})
    assert resp.status_code == 200


# ── GET /api/config ───────────────────────────────────────────────────────────

def test_config_returns_200(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200


def test_config_no_auth_required(client):
    resp = client.get("/api/config", headers={})
    assert resp.status_code == 200


def test_config_returns_dict(client):
    body = client.get("/api/config").json()
    assert isinstance(body, dict)


# ── GET /api/status ───────────────────────────────────────────────────────────

def test_status_returns_200(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200


def test_status_fields(client):
    body = client.get("/api/status").json()
    assert "agent_status" in body
    assert "uptime_seconds" in body
    assert "timestamp" in body


def test_status_dry_run_mode(client):
    body = client.get("/api/status").json()
    assert body["agent_status"] == "dry_run"


def test_status_no_auth_required(client):
    """GET /api/status should be accessible without a token."""
    resp = client.get("/api/status", headers={})
    assert resp.status_code == 200


def test_status_uptime_is_numeric(client):
    body = client.get("/api/status").json()
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0


# ── POST /api/pause ───────────────────────────────────────────────────────────

def test_pause_sets_paused_flag(client, shared_state):
    resp = client.post("/api/pause", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_reflected_in_state(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    client.post("/api/pause", headers=_auth())
    async with shared_state.read() as s:
        assert s.paused is True


def test_pause_requires_auth(client):
    resp = client.post("/api/pause")
    assert resp.status_code == 401


def test_pause_rejects_wrong_token(client):
    resp = client.post("/api/pause", headers=_auth("wrong-token"))
    assert resp.status_code == 401


# ── POST /api/resume ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_clears_paused_flag(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    async with shared_state.write() as s:
        s.paused = True

    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    client.post("/api/resume", headers=_auth())
    async with shared_state.read() as s:
        assert s.paused is False


# ── POST /api/mode ────────────────────────────────────────────────────────────

def test_mode_dry_run_allowed_without_token(client):
    """Switching back to dry_run (live=False) doesn't need confirmation_token."""
    resp = client.post(
        "/api/mode",
        json={"live": False, "confirmation_token": ""},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "dry_run"


def test_mode_live_rejected_wrong_confirmation(client):
    resp = client.post(
        "/api/mode",
        json={"live": True, "confirmation_token": "wrong"},
        headers=_auth(),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_mode_live_accepted_with_correct_token(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    resp = client.post(
        "/api/mode",
        json={"live": True, "confirmation_token": "live-tok"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "live"
    async with shared_state.read() as s:
        assert s.dry_run is False


def test_mode_requires_auth(client):
    resp = client.post("/api/mode", json={"live": False, "confirmation_token": ""})
    assert resp.status_code == 401


# ── CORS ──────────────────────────────────────────────────────────────────────

def test_cors_header_on_health(client):
    resp = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


def test_cors_header_on_status(client):
    resp = client.get("/api/status", headers={"Origin": "http://example.com"})
    assert "access-control-allow-origin" in resp.headers


def test_cors_preflight(client):
    resp = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code in (200, 204)


# ── SharedState (enriched schema) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shared_state_snapshot_fields():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    expected = {
        "timestamp", "btc_price", "agent_status", "uptime_seconds",
        "feeds", "vol_surface", "volatility_regime",
        "signals", "positions", "positions_summary",
        "settlements", "performance", "risk_config",
    }
    assert expected <= set(snap.keys())


@pytest.mark.asyncio
async def test_shared_state_write_visible_in_snapshot():
    state = SharedState(dry_run=True)
    async with state.write() as s:
        s.btc_price = 99_000.0
    snap = await state.snapshot()
    assert snap["btc_price"] == 99_000.0


@pytest.mark.asyncio
async def test_shared_state_paused_reflected_in_status():
    state = SharedState(dry_run=True)
    async with state.write() as s:
        s.paused = True
    snap = await state.snapshot()
    assert snap["agent_status"] == "paused"


@pytest.mark.asyncio
async def test_shared_state_dry_run_reflected():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    assert snap["agent_status"] == "dry_run"


@pytest.mark.asyncio
async def test_shared_state_live_mode_reflected():
    state = SharedState(dry_run=False)
    snap = await state.snapshot()
    assert snap["agent_status"] == "running"


@pytest.mark.asyncio
async def test_shared_state_feeds_is_dict():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    assert isinstance(snap["feeds"], dict)


@pytest.mark.asyncio
async def test_shared_state_signals_is_list():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    assert isinstance(snap["signals"], list)


@pytest.mark.asyncio
async def test_shared_state_positions_is_list():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    assert isinstance(snap["positions"], list)


@pytest.mark.asyncio
async def test_shared_state_risk_config_is_dict():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    assert isinstance(snap["risk_config"], dict)


@pytest.mark.asyncio
async def test_shared_state_timestamp_is_float():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    assert isinstance(snap["timestamp"], float)
    assert snap["timestamp"] > 0


@pytest.mark.asyncio
async def test_shared_state_uptime_increases():
    import asyncio as _asyncio
    state = SharedState(dry_run=True)
    snap1 = await state.snapshot()
    await _asyncio.sleep(0.05)
    snap2 = await state.snapshot()
    assert snap2["uptime_seconds"] >= snap1["uptime_seconds"]


@pytest.mark.asyncio
async def test_shared_state_concurrent_writes_safe():
    """Multiple concurrent writes should not deadlock or corrupt state."""
    state = SharedState()

    async def _write(val: float) -> None:
        async with state.write() as s:
            s.btc_price = val
            await asyncio.sleep(0)

    await asyncio.gather(*[_write(float(i)) for i in range(20)])
    snap = await state.snapshot()
    assert snap["btc_price"] is not None


# ── WebSocket snapshot ────────────────────────────────────────────────────────

def test_ws_snapshot_sends_json(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    with client.websocket_connect("/ws/snapshot") as ws:
        data = ws.receive_text()
        snap = json.loads(data)
        assert "agent_status" in snap
        assert "timestamp" in snap


def test_ws_snapshot_contains_all_required_keys(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    with client.websocket_connect("/ws/snapshot") as ws:
        snap = json.loads(ws.receive_text())
    required = {
        "agent_status", "btc_price", "feeds", "vol_surface",
        "signals", "positions", "volatility_regime", "risk_config",
        "uptime_seconds", "timestamp",
    }
    assert required <= set(snap.keys())


def test_ws_snapshot_agent_status_valid(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    with client.websocket_connect("/ws/snapshot") as ws:
        snap = json.loads(ws.receive_text())
    assert snap["agent_status"] in ("running", "paused", "dry_run")


# ── Goal-2: run-mode indicator (paper / live / replay) ──────────────────────

@pytest.mark.asyncio
async def test_snapshot_mode_paper():
    snap = await SharedState(dry_run=True).snapshot()
    assert snap["mode"] == "paper"


@pytest.mark.asyncio
async def test_snapshot_mode_live():
    snap = await SharedState(dry_run=False).snapshot()
    assert snap["mode"] == "live"


@pytest.mark.asyncio
async def test_snapshot_mode_replay():
    snap = await SharedState(dry_run=True, replay_mode=True).snapshot()
    assert snap["mode"] == "replay"


@pytest.mark.asyncio
async def test_snapshot_mode_follows_dry_run_toggle():
    # Mode is derived (not stored), so toggling dry_run via the live/dry
    # control flips the operator-facing mode without a restart.
    state = SharedState(dry_run=True)
    assert (await state.snapshot())["mode"] == "paper"
    async with state.write() as s:
        s.dry_run = False
    assert (await state.snapshot())["mode"] == "live"


def test_status_includes_mode(client):
    body = client.get("/api/status").json()
    assert body["mode"] == "paper"


def test_spa_served_at_root(client):
    # The launcher (dashboard.bat) opens the browser at "/"; the static SPA
    # must be served there so a double-click lands on the dashboard.
    r = client.get("/")
    assert r.status_code == 200
    assert "BTC PM Arb Dashboard" in r.text
