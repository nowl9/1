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


# ── Round 9d2 Commit 2: series allow-list fan-out ─────────────────────────────


class TestDiscoverySeriesFanout:
    """``_discover_markets`` issues one GET per allow-listed series ticker
    and union-merges live rows into ``self._tracked``.

    The pre-9d2 implementation pinned discovery to ``series_ticker=KXBTC``
    (a server-side EXACT match) which is the intraday "Bitcoin range"
    series — not what this agent trades.  The 9d2 verify report
    identified KXBTCMINMON / KXBTCMAXMON / KXBTCMAX150 as the live,
    in-window threshold-binary series, with KXBTCW / KXBTCMAXW / BTC /
    BTCD historically active but currently empty.
    """

    def _feed(self) -> KalshiFeed:
        feed = KalshiFeed(
            base_url="https://demo-api.kalshi.co/trade-api/v2",
            key_path="/nonexistent.pem",
            key_id="dummy",
        )
        feed._private_key = None
        return feed

    def _far_future(self) -> str:
        from datetime import datetime, timedelta, timezone

        return (
            datetime.now(timezone.utc) + timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    @pytest.mark.asyncio
    async def test_fan_out_issues_one_request_per_series(self) -> None:
        """Each allow-list ticker gets exactly one GET; no extras."""
        from btc_pm_arb.feeds.kalshi import _BTC_SERIES_TICKERS

        requested: list[str] = []

        async def fake_get(path: str, headers: dict[str, str]) -> Any:
            requested.append(path)
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            resp.json = lambda: {"markets": []}
            return resp

        feed = self._feed()
        feed._client = AsyncMock()
        feed._client.get = fake_get

        await feed._discover_markets()

        # One GET per series ticker, scoped via series_ticker=...
        assert len(requested) == len(_BTC_SERIES_TICKERS)
        for series in _BTC_SERIES_TICKERS:
            assert any(
                f"series_ticker={series}" in p for p in requested
            ), f"expected a GET for series_ticker={series}; got {requested!r}"

    @pytest.mark.asyncio
    async def test_results_union_merged_across_series(self) -> None:
        """Rows from different series ticker responses union-merge into
        a single ``_tracked`` dict."""
        future = self._far_future()

        # Map each series ticker to a distinct contract; the merged
        # _tracked must contain all of them.
        responses_by_series: dict[str, list[dict]] = {
            "KXBTCMINMON": [
                {
                    "ticker": "KXBTCMINMON-26JUN-T80000",
                    "close_time": future,
                    "series_ticker": "KXBTCMINMON",
                }
            ],
            "KXBTCMAXMON": [
                {
                    "ticker": "KXBTCMAXMON-26JUN-T120000",
                    "close_time": future,
                    "series_ticker": "KXBTCMAXMON",
                }
            ],
            "KXBTCMAX150": [
                {
                    "ticker": "KXBTCMAX150-26JUN-T150000",
                    "close_time": future,
                    "series_ticker": "KXBTCMAX150",
                }
            ],
        }

        async def fake_get(path: str, headers: dict[str, str]) -> Any:
            series = next(
                (s for s in responses_by_series if f"series_ticker={s}" in path),
                None,
            )
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            resp.json = lambda: {"markets": responses_by_series.get(series, [])}
            return resp

        feed = self._feed()
        feed._client = AsyncMock()
        feed._client.get = fake_get

        await feed._discover_markets()

        assert "KXBTCMINMON-26JUN-T80000" in feed._tracked
        assert "KXBTCMAXMON-26JUN-T120000" in feed._tracked
        assert "KXBTCMAX150-26JUN-T150000" in feed._tracked

    @pytest.mark.asyncio
    async def test_empty_tier2_response_is_not_an_error(self) -> None:
        """A series ticker returning zero markets (the Tier-2 case
        today) is a normal outcome — not an exception, not a log-error,
        just an empty contribution to the union."""
        future = self._far_future()

        async def fake_get(path: str, headers: dict[str, str]) -> Any:
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            if "series_ticker=KXBTCMINMON" in path:
                resp.json = lambda: {
                    "markets": [
                        {
                            "ticker": "KXBTCMINMON-LIVE",
                            "close_time": future,
                            "series_ticker": "KXBTCMINMON",
                        }
                    ]
                }
            else:
                # All other series tickers (Tier-2 + the rest of Tier-1)
                # return empty — currently the realistic case for Tier-2.
                resp.json = lambda: {"markets": []}
            return resp

        feed = self._feed()
        feed._client = AsyncMock()
        feed._client.get = fake_get

        # The call should complete normally and yield exactly the one
        # live contract from KXBTCMINMON.
        await feed._discover_markets()

        assert feed._tracked == {
            "KXBTCMINMON-LIVE": {
                "ticker": "KXBTCMINMON-LIVE",
                "close_time": future,
                "series_ticker": "KXBTCMINMON",
            }
        }

    @pytest.mark.asyncio
    async def test_single_series_failure_does_not_sink_others(self) -> None:
        """A GET that raises for one series ticker leaves the other
        series' live contracts intact in ``_tracked``."""
        future = self._far_future()

        async def fake_get(path: str, headers: dict[str, str]) -> Any:
            if "series_ticker=KXBTCMAXMON" in path:
                # Simulate a transient 500.
                raise RuntimeError("simulated upstream failure")
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            if "series_ticker=KXBTCMINMON" in path:
                resp.json = lambda: {
                    "markets": [
                        {
                            "ticker": "KXBTCMINMON-OK",
                            "close_time": future,
                            "series_ticker": "KXBTCMINMON",
                        }
                    ]
                }
            else:
                resp.json = lambda: {"markets": []}
            return resp

        feed = self._feed()
        feed._client = AsyncMock()
        feed._client.get = fake_get

        # Must not raise — the failure is per-series, isolated.
        await feed._discover_markets()

        assert "KXBTCMINMON-OK" in feed._tracked

    @pytest.mark.asyncio
    async def test_excluded_intraday_series_never_tracked(self) -> None:
        """Defence-in-depth: even if a mocked response carries rows
        whose ``series_ticker`` is one of the EXCLUDED intraday series
        (KXBTC / KXBTCD / KXBTC15M), they must not enter ``_tracked``.

        Guards against a future change to the allow-list accidentally
        re-introducing the pre-9d2 N=0 failure mode.
        """
        future = self._far_future()

        async def fake_get(path: str, headers: dict[str, str]) -> Any:
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            # Pretend the API also returned a row with an EXCLUDED
            # series tag (the intraday range series).  Even though it
            # would never appear in a series_ticker=KXBTCMINMON response
            # in reality, simulate the worst case where Kalshi's filter
            # leaks: the client must drop it.
            resp.json = lambda: {
                "markets": [
                    {
                        "ticker": "KXBTCMINMON-LEGIT",
                        "close_time": future,
                        "series_ticker": "KXBTCMINMON",
                    },
                    {
                        "ticker": "KXBTC-RANGE-LEAK",
                        "close_time": future,
                        "series_ticker": "KXBTC",
                    },
                    {
                        "ticker": "KXBTCD-INTRADAY-LEAK",
                        "close_time": future,
                        "series_ticker": "KXBTCD",
                    },
                    {
                        "ticker": "KXBTC15M-INTRADAY-LEAK",
                        "close_time": future,
                        "series_ticker": "KXBTC15M",
                    },
                ]
            }
            return resp

        feed = self._feed()
        feed._client = AsyncMock()
        feed._client.get = fake_get

        await feed._discover_markets()

        assert "KXBTCMINMON-LEGIT" in feed._tracked
        assert "KXBTC-RANGE-LEAK" not in feed._tracked
        assert "KXBTCD-INTRADAY-LEAK" not in feed._tracked
        assert "KXBTC15M-INTRADAY-LEAK" not in feed._tracked

    def test_allow_list_excludes_intraday_series_at_constants(self) -> None:
        """The allow-list constants themselves must not contain the
        intraday/range series — a static guard against the configuration
        regression that caused the pre-9d2 zero-signal smoke."""
        from btc_pm_arb.feeds.kalshi import (
            _BTC_SERIES_TICKERS,
            _BTC_SERIES_TICKERS_TIER1,
            _BTC_SERIES_TICKERS_TIER2,
        )

        forbidden = {"KXBTC", "KXBTCD", "KXBTC15M"}
        assert not forbidden & set(_BTC_SERIES_TICKERS_TIER1)
        assert not forbidden & set(_BTC_SERIES_TICKERS_TIER2)
        assert not forbidden & set(_BTC_SERIES_TICKERS)
        # And the load-bearing Tier-1 series the verify report flagged
        # are present.
        for required in ("KXBTCMINMON", "KXBTCMAXMON", "KXBTCMAX150"):
            assert required in _BTC_SERIES_TICKERS_TIER1


# ── C4: empty-series pruning + read-budget spacing + 429 backoff ───────────────

class _FakeResp:
    """Minimal stand-in for an httpx.Response used by the C4 tests."""

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self.content = b"{}"
        self._body = body if body is not None else {"markets": []}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._body


def _c4_feed() -> KalshiFeed:
    feed = KalshiFeed(
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        key_path="/nonexistent.pem",
        key_id="dummy",
    )
    feed._private_key = None
    feed._client = AsyncMock()
    return feed


def test_active_series_excludes_empty_tier2() -> None:
    """The active poll/discovery set is Tier-1 only; the empty Tier-2 series
    (KXBTCW / KXBTCMAXW / BTC / BTCD) are not polled (C4)."""
    from btc_pm_arb.feeds.kalshi import (
        _BTC_SERIES_TICKERS,
        _BTC_SERIES_TICKERS_TIER1,
        _BTC_SERIES_TICKERS_TIER2,
    )

    assert _BTC_SERIES_TICKERS == _BTC_SERIES_TICKERS_TIER1
    for empty in _BTC_SERIES_TICKERS_TIER2:
        assert empty not in _BTC_SERIES_TICKERS


@pytest.mark.asyncio
async def test_discover_markets_does_not_poll_empty_tier2_series() -> None:
    """_discover_markets issues no GET for the empty Tier-2 series (C4)."""
    from btc_pm_arb.feeds.kalshi import _BTC_SERIES_TICKERS_TIER2

    requested: list[str] = []

    async def fake_get(path: str, headers: dict[str, str]) -> Any:
        requested.append(path)
        return _FakeResp(200, {"markets": []})

    feed = _c4_feed()
    feed._client.get = fake_get
    await feed._discover_markets()

    for empty in _BTC_SERIES_TICKERS_TIER2:
        assert not any(f"series_ticker={empty}" in p for p in requested), (
            f"must not poll empty series {empty}; got {requested!r}"
        )


@pytest.mark.asyncio
async def test_http_get_retries_on_429_then_succeeds(monkeypatch) -> None:
    """A 429 (no Retry-After) is self-clock-backed-off and retried (C4)."""
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("btc_pm_arb.feeds.kalshi.asyncio.sleep", fake_sleep)

    calls = {"n": 0}

    async def fake_get(path: str, headers: dict[str, str]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp(429)
        return _FakeResp(200, {"ok": True})

    feed = _c4_feed()
    feed._client.get = fake_get

    body = await feed._http_get("/markets/X/orderbook")

    assert body == {"ok": True}
    assert calls["n"] == 2                      # retried exactly once
    assert 1.0 in slept                         # first backoff was the 1s base


@pytest.mark.asyncio
async def test_rate_gate_reserves_sequential_slots(monkeypatch) -> None:
    """Consecutive GETs are spaced by at least the read-budget interval (C4)."""
    from btc_pm_arb.feeds.kalshi import _READ_SPACING_S

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("btc_pm_arb.feeds.kalshi.asyncio.sleep", fake_sleep)

    feed = _c4_feed()
    await feed._rate_gate()
    first = feed._next_get_at
    await feed._rate_gate()
    second = feed._next_get_at

    assert second - first >= _READ_SPACING_S - 1e-9
    # The second call had to wait ~one spacing interval before its slot.
    assert any(s >= _READ_SPACING_S * 0.5 for s in slept)
