"""Tests for server/app.py and server/state.py — dashboard backend.

Tests cover:
  - GET /api/status returns current agent state
  - WebSocket /ws/snapshot pushes JSON snapshot
  - POST /api/pause and /api/resume toggle paused flag
  - POST /api/risk-config validates and merges config updates
  - POST /api/mode rejects live mode with wrong token
  - Auth: control endpoints reject missing/wrong bearer token
  - SharedState snapshot serialization
  - SharedState concurrent read/write safety
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


# ── GET /api/status ───────────────────────────────────────────────────────────

def test_status_returns_200(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200


def test_status_fields(client):
    body = client.get("/api/status").json()
    assert "status" in body
    assert "uptime_s" in body
    assert "timestamp" in body


def test_status_dry_run_mode(client):
    body = client.get("/api/status").json()
    assert body["status"] == "dry_run"


def test_status_no_auth_required(client):
    """GET /api/status should be accessible without a token."""
    resp = client.get("/api/status", headers={})
    assert resp.status_code == 200


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


# ── POST /api/risk-config ─────────────────────────────────────────────────────

def test_risk_config_update(client, shared_state):
    resp = client.post(
        "/api/risk-config",
        json={"max_position_per_contract_usd": 750.0, "min_confidence": 0.55},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["risk_config"]["max_position_per_contract_usd"] == 750.0
    assert body["risk_config"]["min_confidence"] == 0.55


def test_risk_config_partial_update(client):
    """Sending only one field should not wipe others."""
    # First set two fields
    client.post(
        "/api/risk-config",
        json={"max_total_exposure_usd": 8000.0, "min_confidence": 0.45},
        headers=_auth(),
    )
    # Then update only one
    resp = client.post(
        "/api/risk-config",
        json={"min_confidence": 0.50},
        headers=_auth(),
    )
    body = resp.json()
    assert body["risk_config"]["min_confidence"] == 0.50
    assert body["risk_config"]["max_total_exposure_usd"] == 8000.0


def test_risk_config_invalid_confidence_rejected(client):
    resp = client.post(
        "/api/risk-config",
        json={"min_confidence": 1.5},   # > 1.0
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_risk_config_requires_auth(client):
    resp = client.post("/api/risk-config", json={"min_confidence": 0.5})
    assert resp.status_code == 401


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


# ── SharedState ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shared_state_snapshot_fields():
    state = SharedState(dry_run=True)
    snap = await state.snapshot()
    expected = {"status", "uptime_s", "btc_price", "feed_health", "vol_surface",
                "signals", "positions", "settlement_history", "risk_config",
                "realized_vol", "vol_regime", "timestamp"}
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
    assert snap["status"] == "paused"


@pytest.mark.asyncio
async def test_shared_state_live_mode_reflected():
    state = SharedState(dry_run=False)
    snap = await state.snapshot()
    assert snap["status"] == "live"


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


# ── WebSocket snapshot (basic) ────────────────────────────────────────────────

def test_ws_snapshot_sends_json(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    with client.websocket_connect("/ws/snapshot") as ws:
        data = ws.receive_text()
        snap = json.loads(data)
        assert "status" in snap
        assert "timestamp" in snap


def test_ws_snapshot_contains_all_required_keys(shared_state: SharedState, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    app = create_app(shared_state=shared_state)
    client = TestClient(app)
    with client.websocket_connect("/ws/snapshot") as ws:
        snap = json.loads(ws.receive_text())
    required = {"status", "btc_price", "feed_health", "vol_surface", "signals",
                "positions", "vol_regime", "risk_config"}
    assert required <= set(snap.keys())
