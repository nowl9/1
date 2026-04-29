"""Shared pytest fixtures."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fake WebSocket ────────────────────────────────────────────────────────────

class FakeWebSocket:
    """Minimal async WebSocket stub for unit testing without network access."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [json.dumps(m) for m in messages]
        self._idx = 0
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        if self._idx >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


def make_instrument_response(rpc_id: int, instruments: list[str]) -> dict[str, Any]:
    """Build a JSON-RPC response for public/get_instruments."""
    items = []
    for name in instruments:
        parts = name.split("-")
        strike = float(parts[2]) if len(parts) >= 4 else 50000.0
        items.append({
            "instrument_name": name,
            "strike": strike,
            "kind": "option",
            "is_active": True,
        })
    return {"jsonrpc": "2.0", "id": rpc_id, "result": items}


def make_subscribe_response(rpc_id: int, channels: list[str]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": channels}


def make_ticker_notification(instrument: str, underlying: float = 62000.0) -> dict[str, Any]:
    """Build a synthetic ticker subscription notification."""
    ts_ms = int(datetime(2024, 4, 26, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    strike = float(instrument.split("-")[2])
    kind = instrument.split("-")[-1]
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": f"ticker.{instrument}.raw",
            "data": {
                "instrument_name": instrument,
                "best_bid_price": 0.0230,
                "best_ask_price": 0.0260,
                "mark_price": 0.0245,
                "bid_iv": 63.5,
                "ask_iv": 68.1,
                "mark_iv": 65.8,
                "underlying_price": underlying,
                "index_price": underlying,
                "open_interest": 1234.0,
                "timestamp": ts_ms,
                "greeks": {
                    "delta": 0.35 if kind == "C" else -0.65,
                    "gamma": 0.00002,
                    "vega": 35.0,
                    "theta": -42.0,
                    "rho": 0.5,
                },
            },
        },
    }
