"""Tests for PolymarketFeed.

Scope per Round 7b plan: URL-construction regression (Round 7a regression
class) and the BTC binary-threshold filter.  Token-resolution and
tick-construction tests are deferred to a future round once we've seen
real Polymarket responses to ground fixtures.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from btc_pm_arb.feeds.polymarket import (
    PolymarketFeed,
    _is_btc_binary_threshold,
)


@pytest.mark.asyncio
async def test_request_paths_are_relative_to_base_urls() -> None:
    """Regression — Round 7a doubled-prefix class.

    settings.polymarket_gamma_url and settings.polymarket_clob_url operate
    at host root with no path suffix.  Request paths in PolymarketFeed
    must therefore be written as '/markets' and '/book?...', not
    'https://gamma-api.polymarket.com/markets' (absolute URL — would
    bypass base_url) or '/gamma-api/markets' (would imply absorbing the
    host into the path, the same shape of bug that produced the doubled
    '/trade-api/v2/trade-api/v2/' URLs in KalshiFeed before Round 7a's
    fix).
    """
    gamma_paths: list[str] = []
    clob_paths: list[str] = []

    async def capture_gamma(path: str) -> Any:
        gamma_paths.append(path)
        resp = AsyncMock()
        resp.raise_for_status = lambda: None
        # One market that survives _is_btc_binary_threshold so we
        # actually have something to drive a /book lookup with on the
        # next step.  The test only inspects captured paths; the poll
        # loop is never started.
        resp.json = lambda: {
            "data": [
                {
                    "active": True,
                    "closed": False,
                    "question": "Will Bitcoin reach $100,000 by Jun 30?",
                    "outcomes": ["Yes", "No"],
                    "clobTokenIds": ["yes-tok", "no-tok"],
                    "endDate": "2026-06-30T00:00:00Z",
                }
            ]
        }
        return resp

    async def capture_clob(path: str) -> Any:
        clob_paths.append(path)
        resp = AsyncMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"bids": [], "asks": []}
        return resp

    feed = PolymarketFeed(
        gamma_url="https://gamma-api.polymarket.com",
        clob_url="https://clob.polymarket.com",
    )
    # Inject stub clients so we never hit the network and can capture
    # every path the feed asks each client to GET.
    feed._gamma_client = AsyncMock()
    feed._gamma_client.get = capture_gamma
    feed._clob_client = AsyncMock()
    feed._clob_client.get = capture_clob

    # Discovery populates _tracked, then drive a single poll-loop
    # iteration body inline (the loop's asyncio.sleep makes calling
    # _poll_loop directly awkward in a unit test).
    await feed._discover_markets()
    for yes_token_id, _meta in list(feed._tracked.items()):
        await feed._http_get(
            feed._clob_client, f"/book?token_id={yes_token_id}"
        )

    assert gamma_paths, "expected at least one gamma-API request"
    assert clob_paths, "expected at least one CLOB /book request"

    for path in gamma_paths + clob_paths:
        assert not path.startswith("http://"), (
            f"path {path!r} is an absolute URL — would bypass base_url"
        )
        assert not path.startswith("https://"), (
            f"path {path!r} is an absolute URL — would bypass base_url"
        )
        # Polymarket APIs operate at host root — paths must NOT prepend
        # '/gamma-api/' or '/clob/' (Round 7a regression class).
        assert not path.startswith("/gamma-api/"), (
            f"path {path!r} prepends /gamma-api/ — base_url already "
            f"resolves the host; this would produce a doubled prefix."
        )
        assert not path.startswith("/clob/"), (
            f"path {path!r} prepends /clob/ — base_url already "
            f"resolves the host; this would produce a doubled prefix."
        )
        assert path.startswith("/markets") or path.startswith("/book"), (
            f"path {path!r} is neither /markets... nor /book... — every "
            f"request from PolymarketFeed should be one of those two."
        )


class TestIsBtcBinaryThreshold:
    """Filter accepts BTC binary YES/NO threshold markets and rejects all else."""

    def _base(self, **overrides: object) -> dict:
        market: dict = {
            "active": True,
            "closed": False,
            "question": "Will Bitcoin reach $100,000 by Apr 30?",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-tok", "no-tok"],
            "endDate": "2026-04-30T00:00:00Z",
        }
        market.update(overrides)
        return market

    def test_valid_btc_binary_threshold_market(self) -> None:
        assert _is_btc_binary_threshold(self._base()) is True

    def test_btc_but_not_threshold_question_rejected(self) -> None:
        # Open-ended question — no extractable USD threshold.
        market = self._base(
            question="Will Bitcoin be the #1 crypto by 2026?",
        )
        assert _is_btc_binary_threshold(market) is False

    def test_threshold_but_not_btc_rejected(self) -> None:
        # ETH threshold market — wrong asset.
        market = self._base(
            question="Will ETH reach $5,000 by Q3?",
        )
        assert _is_btc_binary_threshold(market) is False

    def test_closed_market_rejected(self) -> None:
        market = self._base(closed=True)
        assert _is_btc_binary_threshold(market) is False

    def test_json_encoded_outcomes_and_clob_token_ids_accepted(self) -> None:
        """Polymarket gamma returns these as JSON strings, not lists.

        Defensive decode in _coerce_to_list should accept both shapes.
        Regression for the tracked=0 issue caught in Round 7b runtime
        observation on commit 8260a11.
        """
        market = self._base(
            outcomes='["Yes", "No"]',
            clobTokenIds='["yes-tok", "no-tok"]',
        )
        assert _is_btc_binary_threshold(market) is True
