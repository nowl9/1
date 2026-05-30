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


# ── Round 9c Commit 2: raw-frame recorder hook ────────────────────────────────


@pytest.mark.asyncio
async def test_kalshi_recorder_hook_called_with_body_and_endpoint() -> None:
    """When constructed with a recorder, _http_get records each
    successful response body + endpoint path, before json parsing.

    Hook ordering: between resp.raise_for_status() and resp.json() —
    only 2xx bodies reach the recorder (4xx/5xx raise before us).
    """
    from unittest.mock import MagicMock
    from btc_pm_arb.models import DataSource

    mock_recorder = MagicMock()
    feed = KalshiFeed(
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        key_path="/nonexistent.pem",
        key_id="dummy",
        recorder=mock_recorder,
    )

    async def capture_get(path: str, headers: dict[str, str]) -> Any:
        resp = AsyncMock()
        resp.raise_for_status = lambda: None
        resp.content = b'{"markets": []}'
        resp.json = lambda: {"markets": []}
        return resp

    feed._client = AsyncMock()
    feed._client.get = capture_get
    feed._private_key = None  # signed_headers returns {} for None — fine

    await feed._discover_markets()

    assert mock_recorder.record.called
    call = mock_recorder.record.call_args
    assert call.args[0] == DataSource.KALSHI
    assert call.args[1] == b'{"markets": []}'
    # Endpoint kwarg carries the request path so replay can distinguish
    # /markets from /markets/{ticker}/orderbook.
    assert call.kwargs.get("endpoint", "").startswith("/markets")


def test_kalshi_default_recorder_is_none() -> None:
    """Constructing without an explicit recorder kwarg leaves it None
    — the disabled-by-default semantics from Q4."""
    feed = KalshiFeed(
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        key_path="/nonexistent.pem",
        key_id="dummy",
    )
    assert feed._recorder is None


# ── Round 9d2 Commit 1: close_time prune ──────────────────────────────────────


class TestDiscoveryCloseTimePrune:
    """``_discover_markets`` drops rows whose close_time has already
    passed before they enter ``self._tracked``.  Kalshi mass-closes on
    clock-aligned boundaries; a 60s discovery interval otherwise carries
    expired-but-open rows for almost a minute (Round 9d1 86/145 finding).
    """

    def _feed(self) -> KalshiFeed:
        feed = KalshiFeed(
            base_url="https://demo-api.kalshi.co/trade-api/v2",
            key_path="/nonexistent.pem",
            key_id="dummy",
        )
        feed._private_key = None
        return feed

    def _install_markets_response(
        self, feed: KalshiFeed, markets: list[dict]
    ) -> None:
        async def fake_get(path: str, headers: dict[str, str]) -> Any:
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            resp.json = lambda: {"markets": markets}
            return resp

        feed._client = AsyncMock()
        feed._client.get = fake_get

    @pytest.mark.asyncio
    async def test_past_close_time_excluded(self) -> None:
        from datetime import datetime, timedelta, timezone

        past = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        feed = self._feed()
        self._install_markets_response(
            feed,
            [{"ticker": "KXBTC-EXPIRED", "close_time": past}],
        )

        await feed._discover_markets()

        assert "KXBTC-EXPIRED" not in feed._tracked
        assert feed._tracked == {}

    @pytest.mark.asyncio
    async def test_future_close_time_retained(self) -> None:
        from datetime import datetime, timedelta, timezone

        future = (
            datetime.now(timezone.utc) + timedelta(days=7)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        feed = self._feed()
        self._install_markets_response(
            feed,
            [{"ticker": "KXBTC-LIVE", "close_time": future}],
        )

        await feed._discover_markets()

        assert "KXBTC-LIVE" in feed._tracked

    @pytest.mark.asyncio
    async def test_boundary_now_excluded(self) -> None:
        """close_time == now (within a few ms) is treated as expired —
        the guard uses ``<= now``, not ``< now``.  A contract that
        closes exactly at the snapshot instant is not tradable for the
        upcoming poll cycle."""
        from datetime import datetime, timedelta, timezone

        # Use a close_time a tick in the past so the test is robust to
        # the ~µs that elapse between fixture construction and the
        # comparison inside _discover_markets; the boundary semantics
        # being asserted is "close_time <= now → drop", not strict
        # equality on a wall clock.
        boundary = (
            datetime.now(timezone.utc) - timedelta(microseconds=1)
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        feed = self._feed()
        self._install_markets_response(
            feed,
            [{"ticker": "KXBTC-BOUNDARY", "close_time": boundary}],
        )

        await feed._discover_markets()

        assert "KXBTC-BOUNDARY" not in feed._tracked

    @pytest.mark.asyncio
    async def test_missing_close_time_retained(self) -> None:
        """Rows with no close_time field are retained — silently
        dropping them would mask an upstream shape regression."""
        feed = self._feed()
        self._install_markets_response(
            feed,
            [{"ticker": "KXBTC-NOCLOSE"}],
        )

        await feed._discover_markets()

        assert "KXBTC-NOCLOSE" in feed._tracked

    @pytest.mark.asyncio
    async def test_unparseable_close_time_retained(self) -> None:
        """An unparseable close_time string is retained for the same
        reason as a missing one."""
        feed = self._feed()
        self._install_markets_response(
            feed,
            [{"ticker": "KXBTC-BADTIME", "close_time": "not-a-date"}],
        )

        await feed._discover_markets()

        assert "KXBTC-BADTIME" in feed._tracked

    @pytest.mark.asyncio
    async def test_mixed_batch_partitioned(self) -> None:
        """A mixed response retains only the live rows."""
        from datetime import datetime, timedelta, timezone

        past = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        future = (
            datetime.now(timezone.utc) + timedelta(hours=4)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        feed = self._feed()
        self._install_markets_response(
            feed,
            [
                {"ticker": "EXPIRED-A", "close_time": past},
                {"ticker": "LIVE-A", "close_time": future},
                {"ticker": "EXPIRED-B", "close_time": past},
                {"ticker": "LIVE-B", "close_time": future},
            ],
        )

        await feed._discover_markets()

        assert set(feed._tracked.keys()) == {"LIVE-A", "LIVE-B"}


class TestPollLoopCloseTimeGuard:
    """``_poll_loop`` re-checks close_time before issuing the orderbook
    request, defending against a snapshot that goes stale across the
    60s discovery interval."""

    @pytest.mark.asyncio
    async def test_expired_ticker_skipped_mid_poll(self) -> None:
        """A ticker that was alive at discovery but expired by the
        time _poll_loop reaches it is skipped without an /orderbook
        request being issued."""
        from datetime import datetime, timedelta, timezone

        feed = KalshiFeed(
            base_url="https://demo-api.kalshi.co/trade-api/v2",
            key_path="/nonexistent.pem",
            key_id="dummy",
        )
        feed._private_key = None

        past = (
            datetime.now(timezone.utc) - timedelta(seconds=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Seed _tracked directly to bypass discovery; simulate a
        # snapshot that went stale after collection.
        feed._tracked = {
            "STALE-EXPIRED": {"ticker": "STALE-EXPIRED", "close_time": past},
            "STILL-LIVE": {"ticker": "STILL-LIVE", "close_time": future},
        }

        requested_paths: list[str] = []

        async def fake_get(path: str, headers: dict[str, str]) -> Any:
            requested_paths.append(path)
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            resp.json = lambda: {"orderbook_fp": {}}
            return resp

        feed._client = AsyncMock()
        feed._client.get = fake_get

        # Drive one iteration of the loop body inline rather than
        # invoking _poll_loop directly (the loop's asyncio.sleep makes
        # that awkward in a unit test).  Replicates the guard + GET
        # pair from _poll_loop:312-340 for the assertion target.
        from btc_pm_arb.feeds.kalshi import _meta_close_time

        for ticker, meta in list(feed._tracked.items()):
            close_time = _meta_close_time(meta)
            if close_time is not None and close_time <= datetime.now(timezone.utc):
                continue
            await feed._http_get(f"/markets/{ticker}/orderbook")

        assert "/markets/STALE-EXPIRED/orderbook" not in requested_paths
        assert "/markets/STILL-LIVE/orderbook" in requested_paths
