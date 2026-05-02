"""Tests for KalshiFeed.

Coverage:
* URL-construction regression (round 6 verification).
* Dollar-string round-trip regression (Round 7c step 1.5):
  TestBuildTickDollarRoundTrip exercises the post-March-2026 wire
  format end-to-end through ``_build_tick`` → ``normalize_kalshi_tick``,
  catching the silent zero-ticks failure mode observed against
  api.elections.kalshi.com prod.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from btc_pm_arb.feeds.kalshi import KalshiFeed, _build_tick


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


class TestBuildTickDollarRoundTrip:
    """Regression for the March 2026 dollar-string migration.

    Before this test existed, KalshiFeed silently emitted zero ticks for
    5+ minutes against api.elections.kalshi.com because _poll_loop read
    "orderbook" (gone) instead of "orderbook_fp", and _build_tick parsed
    [price_cents, qty] (gone) instead of [price_dollars_str, qty_str].
    """

    def test_orderbook_fp_yes_dollars_round_trip(self) -> None:
        # Mirror the wire format observed against /orderbook on prod.
        book_fp = {
            "yes_dollars": [["0.6100", "50.00"], ["0.6200", "100.00"]],
            "no_dollars": [["0.3500", "200.00"], ["0.3400", "75.00"]],
        }
        meta = {
            "title": "Will BTC be above $100,000 on Dec 31?",
            "subtitle": "BTC above $100,000",
            "close_time": "2026-12-31T23:59:00Z",
        }
        tick = _build_tick("KXBTC-26DEC31-B100000", meta, book_fp)
        assert tick is not None
        assert tick.yes_bid == pytest.approx(0.62)   # max yes_dollars price
        assert tick.no_bid == pytest.approx(0.35)    # max no_dollars price
        assert tick.yes_ask == pytest.approx(0.65)   # 1 - max(no) = 1 - 0.35
        assert tick.no_ask == pytest.approx(0.38)    # 1 - max(yes) = 1 - 0.62
        assert tick.strike == 100_000.0
        assert tick.contract_id == "KXBTC-26DEC31-B100000"

    def test_one_sided_orderbook_emits_complementary_pair(self) -> None:
        """Real prod observation: many Kalshi BTC contracts have one-sided books.

        Probe of prod /orderbook for KXBTC-26MAY0203-T86299.99 returned
        yes_dollars=[] and no_dollars populated. _build_tick must still emit
        a tick — yes_ask is derivable from no_bid via complementary pricing.
        """
        book_fp = {
            "yes_dollars": [],
            "no_dollars": [["0.0100", "33383.00"], ["0.9900", "16456.00"]],
        }
        meta = {
            "title": "Will BTC be above $86,300 on May 2?",
            "subtitle": "BTC above $86,300",
            "close_time": "2026-05-02T16:00:00Z",
        }
        tick = _build_tick("KXBTC-26MAY0203-T86299.99", meta, book_fp)
        assert tick is not None
        assert tick.yes_bid is None              # no yes-side bids
        assert tick.no_bid == pytest.approx(0.99)  # max no_dollars price
        assert tick.yes_ask == pytest.approx(0.01)  # 1 - max(no) = 0.01
        assert tick.no_ask is None                # no yes-side bid → can't derive
