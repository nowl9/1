"""Tests for Deribit instrument parsing and tick handling (no network)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from btc_pm_arb.feeds.deribit import DeribitFeed, parse_instrument
from btc_pm_arb.models import OptionType
from tests.conftest import (
    FakeWebSocket,
    make_instrument_response,
    make_subscribe_response,
    make_ticker_notification,
)


# ── parse_instrument unit tests ───────────────────────────────────────────────

class TestParseInstrument:
    def test_call_option(self) -> None:
        result = parse_instrument("BTC-26APR24-50000-C")
        assert result is not None
        strike, expiry, opt_type = result
        assert strike == 50_000.0
        assert opt_type == OptionType.CALL
        assert expiry == datetime(2024, 4, 26, 8, 0, 0, tzinfo=timezone.utc)

    def test_put_option(self) -> None:
        result = parse_instrument("BTC-26APR24-50000-P")
        assert result is not None
        _, _, opt_type = result
        assert opt_type == OptionType.PUT

    def test_six_digit_strike(self) -> None:
        result = parse_instrument("BTC-31DEC24-100000-C")
        assert result is not None
        strike, expiry, _ = result
        assert strike == 100_000.0
        assert expiry.year == 2024
        assert expiry.month == 12
        assert expiry.day == 31

    def test_single_digit_day(self) -> None:
        result = parse_instrument("BTC-5JAN25-80000-C")
        assert result is not None
        _, expiry, _ = result
        assert expiry.day == 5
        assert expiry.month == 1
        assert expiry.year == 2025

    def test_expiry_hour_is_utc_8(self) -> None:
        result = parse_instrument("BTC-26APR24-50000-C")
        assert result is not None
        _, expiry, _ = result
        assert expiry.hour == 8
        assert expiry.tzinfo == timezone.utc

    def test_invalid_name_returns_none(self) -> None:
        assert parse_instrument("ETH-26APR24-50000-C") is None
        assert parse_instrument("BTC-26APR24-50000-X") is None
        assert parse_instrument("BTC-26ZZZ24-50000-C") is None
        assert parse_instrument("not-an-instrument") is None
        assert parse_instrument("") is None

    def test_all_months(self) -> None:
        months = [
            ("JAN", 1), ("FEB", 2), ("MAR", 3), ("APR", 4),
            ("MAY", 5), ("JUN", 6), ("JUL", 7), ("AUG", 8),
            ("SEP", 9), ("OCT", 10), ("NOV", 11), ("DEC", 12),
        ]
        for abbr, num in months:
            result = parse_instrument(f"BTC-15{abbr}25-50000-C")
            assert result is not None, f"Failed for month {abbr}"
            _, expiry, _ = result
            assert expiry.month == num


# ── DeribitFeed tick handling (pure unit tests, no network) ───────────────────

class TestDeribitFeedTickParsing:
    """Test _handle_ticker by calling it directly on a feed instance."""

    def _make_feed(self) -> DeribitFeed:
        feed = DeribitFeed.__new__(DeribitFeed)
        feed._url = "wss://fake"
        feed._queue = asyncio.Queue(maxsize=1000)
        feed._running = False
        feed._ws = None
        feed._rpc_id = 0
        feed._pending_rpcs = {}
        feed._instrument_cache = {}
        return feed

    def test_valid_call_tick_enqueued(self) -> None:
        feed = self._make_feed()
        ticker = make_ticker_notification("BTC-26APR24-50000-C")
        data = ticker["params"]["data"]
        feed._handle_ticker(data)
        assert feed._queue.qsize() == 1
        tick = feed._queue.get_nowait()
        assert tick.instrument_name == "BTC-26APR24-50000-C"
        assert tick.strike == 50_000.0
        assert tick.option_type == OptionType.CALL
        assert tick.bid == pytest.approx(0.023)
        assert tick.ask == pytest.approx(0.026)
        assert tick.mark_price == pytest.approx(0.0245)
        assert tick.underlying_price == pytest.approx(62_000.0)
        assert tick.greeks is not None
        assert tick.greeks.delta == pytest.approx(0.35)

    def test_valid_put_tick_enqueued(self) -> None:
        feed = self._make_feed()
        data = make_ticker_notification("BTC-26APR24-50000-P")["params"]["data"]
        feed._handle_ticker(data)
        tick = feed._queue.get_nowait()
        assert tick.option_type == OptionType.PUT
        assert tick.greeks is not None
        assert tick.greeks.delta == pytest.approx(-0.65)

    def test_tick_without_underlying_price_is_dropped(self) -> None:
        feed = self._make_feed()
        data: dict[str, Any] = {
            "instrument_name": "BTC-26APR24-50000-C",
            "mark_price": 0.02,
            "timestamp": 1_700_000_000_000,
            "underlying_price": 0.0,
            "index_price": 0.0,
        }
        feed._handle_ticker(data)
        assert feed._queue.qsize() == 0

    def test_tick_with_unknown_instrument_is_dropped(self) -> None:
        feed = self._make_feed()
        data: dict[str, Any] = {
            "instrument_name": "ETH-26APR24-50000-C",
            "mark_price": 0.02,
            "timestamp": 1_700_000_000_000,
            "underlying_price": 3000.0,
            "index_price": 3000.0,
        }
        feed._handle_ticker(data)
        assert feed._queue.qsize() == 0

    def test_tick_with_no_bid_ask_is_accepted(self) -> None:
        """Ticks with no bid/ask (illiquid strikes) should still be accepted."""
        feed = self._make_feed()
        data: dict[str, Any] = {
            "instrument_name": "BTC-26APR24-200000-C",
            "mark_price": 0.0001,
            "timestamp": 1_700_000_000_000,
            "underlying_price": 62_000.0,
            "index_price": 62_000.0,
            # no best_bid_price / best_ask_price keys
        }
        feed._handle_ticker(data)
        assert feed._queue.qsize() == 1
        tick = feed._queue.get_nowait()
        assert tick.bid is None
        assert tick.ask is None

    def test_queue_full_drops_oldest_tick(self) -> None:
        feed = self._make_feed()
        feed._queue = asyncio.Queue(maxsize=2)  # tiny queue
        instruments = [
            "BTC-26APR24-50000-C",
            "BTC-26APR24-60000-C",
            "BTC-26APR24-70000-C",
        ]
        for inst in instruments:
            data = make_ticker_notification(inst)["params"]["data"]
            feed._handle_ticker(data)
        # Queue should still have exactly 2 items (oldest was dropped)
        assert feed._queue.qsize() == 2

    def test_instrument_cache_populated(self) -> None:
        feed = self._make_feed()
        data = make_ticker_notification("BTC-26APR24-50000-C")["params"]["data"]
        feed._handle_ticker(data)
        assert "BTC-26APR24-50000-C" in feed._instrument_cache

    def test_bid_price_usd_computed(self) -> None:
        feed = self._make_feed()
        data = make_ticker_notification("BTC-26APR24-50000-C", underlying=62_000.0)["params"]["data"]
        feed._handle_ticker(data)
        tick = feed._queue.get_nowait()
        expected = pytest.approx(0.023 * 62_000.0, rel=1e-4)
        assert tick.bid_price_usd == expected

    def test_timestamp_is_utc(self) -> None:
        feed = self._make_feed()
        data = make_ticker_notification("BTC-26APR24-50000-C")["params"]["data"]
        feed._handle_ticker(data)
        tick = feed._queue.get_nowait()
        assert tick.timestamp.tzinfo == timezone.utc


# ── Integration-style test: full message dispatch ─────────────────────────────

class TestDeribitFeedMessageDispatch:
    """Test _handle_message routing without network."""

    def _make_feed(self) -> DeribitFeed:
        feed = DeribitFeed.__new__(DeribitFeed)
        feed._url = "wss://fake"
        feed._queue = asyncio.Queue(maxsize=1000)
        feed._running = False
        feed._ws = None
        feed._rpc_id = 0
        feed._pending_rpcs = {}
        feed._instrument_cache = {}
        return feed

    @pytest.mark.asyncio
    async def test_rpc_response_resolves_future(self) -> None:
        feed = self._make_feed()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        feed._pending_rpcs[42] = fut

        msg = {"jsonrpc": "2.0", "id": 42, "result": ["channel1"]}
        await feed._handle_message(msg)

        assert fut.done()
        assert fut.result() == ["channel1"]

    @pytest.mark.asyncio
    async def test_rpc_error_response_sets_exception(self) -> None:
        feed = self._make_feed()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        feed._pending_rpcs[7] = fut

        msg = {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        await feed._handle_message(msg)

        assert fut.done()
        with pytest.raises(RuntimeError, match="RPC error"):
            fut.result()

    @pytest.mark.asyncio
    async def test_subscription_message_enqueues_tick(self) -> None:
        feed = self._make_feed()
        notification = make_ticker_notification("BTC-26APR24-50000-C")
        await feed._handle_message(notification)
        assert feed._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_unknown_method_is_ignored(self) -> None:
        feed = self._make_feed()
        msg = {"jsonrpc": "2.0", "method": "some.unknown.method", "params": {}}
        await feed._handle_message(msg)  # should not raise
        assert feed._queue.qsize() == 0
