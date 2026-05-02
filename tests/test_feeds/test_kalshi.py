"""Tests for KalshiFeed — currently scoped to the URL-construction regression
caught in round 6 verification.

Broader unit-test coverage of orderbook parsing and tick conversion is
deferred to a future round once we've seen real Kalshi /orderbook responses
to ground the fixtures."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from btc_pm_arb.feeds.kalshi import KalshiFeed


@pytest.mark.asyncio
async def test_request_paths_do_not_double_trade_api_prefix() -> None:
    """Regression — round 6 verification.

    settings.kalshi_base_url already contains '/trade-api/v2' by convention;
    the feed's request paths must therefore be written as '/markets', not
    '/trade-api/v2/markets'.  Prepending the prefix in both places produces
    URLs like 'https://demo-api.kalshi.co/trade-api/v2/trade-api/v2/markets'
    which Kalshi 404s on, sending the feed into a permanent reconnect loop.
    """
    captured: list[str] = []

    async def capture_get(path: str, headers: dict[str, str]) -> Any:
        captured.append(path)
        resp = AsyncMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"markets": []}
        return resp

    feed = KalshiFeed(
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        key_path="/nonexistent.pem",
        key_id="dummy",
    )
    # Inject a stub client so we never actually hit the network and so we
    # can capture every path the feed asks the client to GET.
    feed._client = AsyncMock()
    feed._client.get = capture_get
    feed._private_key = None  # signed_headers returns {} for None — fine here

    await feed._discover_markets()

    assert captured, "expected at least one HTTP request from _discover_markets"
    for path in captured:
        assert not path.startswith("/trade-api/v2"), (
            f"path {path!r} starts with /trade-api/v2 — base_url already ends "
            f"in that prefix; this would produce a doubled-prefix 404 at runtime."
        )
