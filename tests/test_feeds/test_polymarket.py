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


# ── Round 9c Commit 2: raw-frame recorder hook ────────────────────────────────


@pytest.mark.asyncio
async def test_polymarket_recorder_hook_called_with_body_and_endpoint() -> None:
    """When constructed with a recorder, _http_get records each
    successful response body + endpoint path, before json parsing.

    Same hook ordering as KalshiFeed — between raise_for_status() and
    resp.json() — so only 2xx bodies are recorded.
    """
    from unittest.mock import MagicMock
    from btc_pm_arb.models import DataSource

    mock_recorder = MagicMock()
    feed = PolymarketFeed(
        gamma_url="https://gamma-api.polymarket.com",
        clob_url="https://clob.polymarket.com",
        recorder=mock_recorder,
    )

    async def capture_gamma(path: str) -> Any:
        resp = AsyncMock()
        resp.raise_for_status = lambda: None
        resp.content = b'{"data": []}'
        resp.json = lambda: {"data": []}
        return resp

    feed._gamma_client = AsyncMock()
    feed._gamma_client.get = capture_gamma

    await feed._discover_markets()

    assert mock_recorder.record.called
    call = mock_recorder.record.call_args
    assert call.args[0] == DataSource.POLYMARKET
    assert call.args[1] == b'{"data": []}'
    # Endpoint kwarg distinguishes gamma /markets vs CLOB /book paths.
    assert call.kwargs.get("endpoint", "").startswith("/markets")


def test_polymarket_default_recorder_is_none() -> None:
    """Constructing without an explicit recorder kwarg leaves it None."""
    feed = PolymarketFeed(
        gamma_url="https://gamma-api.polymarket.com",
        clob_url="https://clob.polymarket.com",
    )
    assert feed._recorder is None


# ── Round 9d2 Commit 3: offset-paginated discovery ────────────────────────────


def _btc_market(
    *,
    market_id: str,
    yes_token: str,
    strike: int = 100_000,
    end_date: str = "2026-12-31T00:00:00Z",
) -> dict:
    """Build a market dict that survives _is_btc_binary_threshold."""
    return {
        "id": market_id,
        "active": True,
        "closed": False,
        "question": f"Will Bitcoin reach ${strike:,} by Dec 31?",
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [yes_token, f"no-{market_id}"],
        "endDate": end_date,
    }


def _filler(market_id: str) -> dict:
    """Build a non-BTC market that's rejected by the filter — represents
    the political / sports volume that crowded out BTC binaries pre-9d2."""
    return {
        "id": market_id,
        "active": True,
        "closed": False,
        "question": "Will Team X win the championship?",
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [f"yes-{market_id}", f"no-{market_id}"],
        "endDate": "2026-06-30T00:00:00Z",
    }


class TestDiscoveryOffsetPagination:
    """``_discover_markets`` walks /markets with offset pagination to
    escape the silent server-side limit=100 clamp.

    The 9d2 verify report found BTC binaries in offset 0..2900 across
    all categories; pre-9d2 the feed asked for limit=500 and silently
    got the top 100 by volume — BTC binaries got crowded out by
    politics/sports and only one (or zero) was tracked.
    """

    def _feed(self) -> PolymarketFeed:
        feed = PolymarketFeed(
            gamma_url="https://gamma-api.polymarket.com",
            clob_url="https://clob.polymarket.com",
        )
        return feed

    def _install_paginated_responder(
        self,
        feed: PolymarketFeed,
        pages: list[list[dict]],
    ) -> list[str]:
        """Mount a gamma client that returns the supplied per-offset
        pages.  Returns a list that captures each requested path."""
        from btc_pm_arb.feeds.polymarket import _DISCOVERY_PAGE_SIZE

        captured: list[str] = []

        async def fake_get(path: str) -> Any:
            captured.append(path)
            # Parse the offset from the query string.
            offset = 0
            for part in path.split("?", 1)[-1].split("&"):
                if part.startswith("offset="):
                    offset = int(part.split("=", 1)[1])
            page_idx = offset // _DISCOVERY_PAGE_SIZE
            page = pages[page_idx] if 0 <= page_idx < len(pages) else []
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            resp.json = lambda: {"data": page}
            return resp

        feed._gamma_client = AsyncMock()
        feed._gamma_client.get = fake_get
        return captured

    @pytest.mark.asyncio
    async def test_pagination_issues_successive_offset_requests(self) -> None:
        """Three full pages of 100 + a fourth short page exercises the
        offset-increment path; offsets are 0, 100, 200, 300."""
        from btc_pm_arb.feeds.polymarket import _DISCOVERY_PAGE_SIZE

        pages = [
            [_filler(f"p0-{i}") for i in range(_DISCOVERY_PAGE_SIZE)],
            [_filler(f"p1-{i}") for i in range(_DISCOVERY_PAGE_SIZE)],
            [_filler(f"p2-{i}") for i in range(_DISCOVERY_PAGE_SIZE)],
            [_btc_market(market_id="btc-tail", yes_token="yes-tail")],
        ]
        feed = self._feed()
        captured = self._install_paginated_responder(feed, pages)

        await feed._discover_markets()

        # Four requests; offsets 0, 100, 200, 300.
        assert len(captured) == 4
        for expected_offset in (0, 100, 200, 300):
            assert any(
                f"offset={expected_offset}" in p for p in captured
            ), f"missing offset={expected_offset} in {captured!r}"
        # The BTC binary at the tail of the volume ordering is now
        # discovered — exactly the symptom the verify report flagged.
        assert "yes-tail" in feed._tracked

    @pytest.mark.asyncio
    async def test_short_page_stops_pagination_early(self) -> None:
        """A page with fewer than ``_DISCOVERY_PAGE_SIZE`` rows ends
        the loop — no further offset requests are issued."""
        from btc_pm_arb.feeds.polymarket import (
            _DISCOVERY_MAX_PAGES,
            _DISCOVERY_PAGE_SIZE,
        )

        pages = [
            [_filler(f"p0-{i}") for i in range(_DISCOVERY_PAGE_SIZE)],
            [_btc_market(market_id="btc-short", yes_token="yes-short")],
            # If pagination didn't short-circuit, these would be requested.
            [_filler(f"p2-{i}") for i in range(_DISCOVERY_PAGE_SIZE)],
        ]
        feed = self._feed()
        captured = self._install_paginated_responder(feed, pages)

        await feed._discover_markets()

        assert len(captured) == 2, (
            f"expected exactly 2 paginated requests (full page + short "
            f"page) but got {len(captured)}: {captured!r}"
        )
        # Sanity: the ceiling never fired, so this is the early-stop
        # path and not the max-pages backstop.
        assert len(captured) < _DISCOVERY_MAX_PAGES
        assert "yes-short" in feed._tracked

    @pytest.mark.asyncio
    async def test_cross_page_dedupe_by_market_id(self) -> None:
        """A market id appearing in two pages contributes a single entry."""
        from btc_pm_arb.feeds.polymarket import _DISCOVERY_PAGE_SIZE

        dup = _btc_market(market_id="btc-dup", yes_token="yes-dup")
        # Pad page 0 with fillers so it's full (forces a second fetch),
        # placing the BTC market at the boundary so it can appear on both.
        page0 = [_filler(f"p0-{i}") for i in range(_DISCOVERY_PAGE_SIZE - 1)] + [dup]
        page1 = [dup, _btc_market(market_id="btc-unique", yes_token="yes-unique")]
        feed = self._feed()
        self._install_paginated_responder(feed, [page0, page1])

        await feed._discover_markets()

        # Both BTC binaries are tracked; the duplicate didn't displace
        # the unique one or get added twice.
        assert "yes-dup" in feed._tracked
        assert "yes-unique" in feed._tracked
        # Sanity-check the keyed-by-id dedupe: exactly the two BTC
        # binaries survive the filter (fillers are rejected by
        # _is_btc_binary_threshold).
        assert set(feed._tracked.keys()) == {"yes-dup", "yes-unique"}

    @pytest.mark.asyncio
    async def test_btc_binary_below_top100_now_discovered(self) -> None:
        """Pre-9d2 a BTC binary sitting below the top-100 volume
        threshold was invisible — limit=500 was clamped server-side and
        the top 100 were dominated by politics/sports.  With offset
        pagination it must be discovered."""
        from btc_pm_arb.feeds.polymarket import _DISCOVERY_PAGE_SIZE

        # Page 0: 100 high-volume non-BTC markets (simulates the
        # politics+sports volume that crowded out BTC pre-fix).
        # Page 1: a BTC binary at the top of the next 100.
        pages = [
            [_filler(f"crowd-{i}") for i in range(_DISCOVERY_PAGE_SIZE)],
            [_btc_market(market_id="btc-below-top100", yes_token="yes-below")],
        ]
        feed = self._feed()
        self._install_paginated_responder(feed, pages)

        await feed._discover_markets()

        assert "yes-below" in feed._tracked, (
            "BTC binary sitting outside the top-100 by volume should "
            "now be discovered via offset pagination — pre-9d2 the "
            "discovery query was silently clamped to limit=100 and "
            "this market was invisible."
        )

    @pytest.mark.asyncio
    async def test_page_ceiling_bounds_unbounded_responses(self) -> None:
        """Defensive: if every page returns a full ``_DISCOVERY_PAGE_SIZE``
        rows (server never short-pages), pagination stops at
        ``_DISCOVERY_MAX_PAGES`` rather than looping forever."""
        from btc_pm_arb.feeds.polymarket import (
            _DISCOVERY_MAX_PAGES,
            _DISCOVERY_PAGE_SIZE,
        )

        captured: list[str] = []

        async def fake_get(path: str) -> Any:
            captured.append(path)
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.content = b""
            resp.json = lambda: {
                # Always return a full page — simulates a misbehaving
                # server that never short-pages.
                "data": [_filler(f"r{len(captured)}-{i}") for i in range(_DISCOVERY_PAGE_SIZE)],
            }
            return resp

        feed = self._feed()
        feed._gamma_client = AsyncMock()
        feed._gamma_client.get = fake_get

        await feed._discover_markets()

        assert len(captured) == _DISCOVERY_MAX_PAGES, (
            f"expected pagination to terminate at the page ceiling "
            f"({_DISCOVERY_MAX_PAGES}) when no page is short; got "
            f"{len(captured)} requests"
        )

    @pytest.mark.asyncio
    async def test_first_page_short_completes_in_one_request(self) -> None:
        """A response shorter than ``_DISCOVERY_PAGE_SIZE`` on the first
        page (the realistic happy-path for a low-traffic gamma instance)
        ends after one request."""
        from btc_pm_arb.feeds.polymarket import _DISCOVERY_PAGE_SIZE

        pages = [
            [
                _btc_market(market_id="btc-only", yes_token="yes-only"),
                _filler("noise-0"),
            ]
        ]
        # Sanity-check the fixture: a one-page short response.
        assert len(pages[0]) < _DISCOVERY_PAGE_SIZE
        feed = self._feed()
        captured = self._install_paginated_responder(feed, pages)

        await feed._discover_markets()

        assert len(captured) == 1
        assert "yes-only" in feed._tracked
