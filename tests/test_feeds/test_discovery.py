"""Tests for feeds/discovery.py — MarketDiscovery and strike extractor.

PMXT sidecar calls are mocked — no live Node.js process required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from btc_pm_arb.feeds.discovery import (
    DiscoveredContract,
    MarketDiscovery,
    extract_strike,
    _is_btc_market,
)

_NOW = datetime.now(timezone.utc)
_EXPIRY = _NOW + timedelta(days=14)


# ── Helpers ───────────────────────────────────────────────────────────────────

@dataclass
class FakeOutcome:
    outcome_id: str
    label: str
    price: float
    price_change_24h: float | None = None
    metadata: dict | None = None
    market_id: str | None = None


@dataclass
class FakeMarket:
    market_id: str
    title: str
    outcomes: list
    volume_24h: float
    liquidity: float
    url: str
    description: str | None = None
    resolution_date: datetime | None = None
    volume: float | None = None
    open_interest: float | None = None


def _btc_market(
    market_id: str = "poly-1",
    title: str = "Will BTC exceed $100,000 by June 28?",
    yes_price: float = 0.42,
) -> FakeMarket:
    return FakeMarket(
        market_id=market_id,
        title=title,
        outcomes=[
            FakeOutcome(outcome_id=f"{market_id}-yes", label="Yes", price=yes_price),
            FakeOutcome(outcome_id=f"{market_id}-no", label="No", price=1 - yes_price),
        ],
        volume_24h=50_000.0,
        liquidity=200_000.0,
        url=f"https://polymarket.com/event/{market_id}",
        resolution_date=_EXPIRY,
    )


# ── extract_strike ────────────────────────────────────────────────────────────

def test_extract_strike_dollar_amount():
    assert extract_strike("BTC above $100,000 by June") == pytest.approx(100_000.0)


def test_extract_strike_k_suffix():
    assert extract_strike("Will BTC reach $100k?") == pytest.approx(100_000.0)


def test_extract_strike_k_uppercase():
    assert extract_strike("BTC above $95K") == pytest.approx(95_000.0)


def test_extract_strike_no_dollar():
    assert extract_strike("Bitcoin price exceeds 105000") == pytest.approx(105_000.0)


def test_extract_strike_returns_none_for_nonbtc():
    assert extract_strike("Will Trump win the election?") is None


def test_extract_strike_ignores_unrealistic_values():
    # $5 is below $1k floor
    assert extract_strike("BTC above $5") is None


def test_extract_strike_uses_description_fallback():
    result = extract_strike("Crypto market question", "Bitcoin price will reach $120,000")
    assert result == pytest.approx(120_000.0)


# ── _is_btc_market ────────────────────────────────────────────────────────────

def test_is_btc_market_positive():
    assert _is_btc_market("Will BTC exceed $100k?")


def test_is_btc_market_bitcoin_word():
    assert _is_btc_market("Bitcoin price above 95000")


def test_is_btc_market_negative():
    assert not _is_btc_market("Will ETH reach $5000?")


def test_is_btc_market_negative_stock():
    assert not _is_btc_market("Will Apple stock exceed $200?")


# ── MarketDiscovery._parse_market ─────────────────────────────────────────────

def test_parse_market_extracts_contract():
    discovery = MarketDiscovery()
    market = _btc_market(title="Will BTC exceed $100,000 by June 28?", yes_price=0.42)
    contracts = discovery._parse_market(market, "polymarket")
    assert len(contracts) == 1
    c = contracts[0]
    assert c.strike_price == pytest.approx(100_000.0)
    assert c.yes_price == pytest.approx(0.42)
    assert c.venue == "polymarket"
    assert c.outcome_id == "poly-1-yes"


def test_parse_market_skips_non_btc():
    discovery = MarketDiscovery()
    market = FakeMarket(
        market_id="eth-1",
        title="Will ETH reach $5,000?",
        outcomes=[FakeOutcome("eth-1-yes", "Yes", 0.30)],
        volume_24h=10_000.0,
        liquidity=50_000.0,
        url="https://polymarket.com/eth-1",
    )
    assert discovery._parse_market(market, "polymarket") == []


def test_parse_market_skips_no_parseable_strike():
    discovery = MarketDiscovery()
    market = FakeMarket(
        market_id="btc-vague",
        title="Will Bitcoin go up?",
        outcomes=[FakeOutcome("btc-vague-yes", "Yes", 0.55)],
        volume_24h=1_000.0,
        liquidity=5_000.0,
        url="https://polymarket.com/btc-vague",
    )
    assert discovery._parse_market(market, "polymarket") == []


def test_parse_market_only_yes_outcome():
    """Only 'Yes'/'Above'/'Up' outcomes should be included."""
    discovery = MarketDiscovery()
    market = _btc_market(title="BTC above $100,000?")
    # Market has Yes and No outcomes; only Yes should be included
    contracts = discovery._parse_market(market, "kalshi")
    assert all(c.yes_price == 0.42 for c in contracts)
    assert len(contracts) == 1


def test_parse_market_expiry_propagated():
    discovery = MarketDiscovery()
    market = _btc_market(title="BTC above $100k?")
    contracts = discovery._parse_market(market, "polymarket")
    assert contracts[0].expiry == _EXPIRY


# ── MarketDiscovery.scan_btc_contracts (mocked sidecar) ──────────────────────

@pytest.mark.asyncio
async def test_scan_returns_empty_when_server_not_ready():
    discovery = MarketDiscovery()
    discovery._server_ok = False
    contracts = await discovery.scan_btc_contracts()
    assert contracts == []


@pytest.mark.asyncio
async def test_scan_returns_contracts_when_sidecar_mocked():
    discovery = MarketDiscovery()
    discovery._server_ok = True

    fake_poly = MagicMock()
    fake_poly.fetch_markets.return_value = [
        _btc_market("poly-1", "BTC above $100,000 by June?", yes_price=0.42),
        _btc_market("poly-2", "BTC above $95k?", yes_price=0.55),
    ]
    fake_kalshi = MagicMock()
    fake_kalshi.fetch_markets.return_value = []

    discovery._poly = fake_poly
    discovery._kalshi = fake_kalshi

    contracts = await discovery.scan_btc_contracts()
    assert len(contracts) == 2
    strikes = {c.strike_price for c in contracts}
    assert 100_000.0 in strikes
    assert 95_000.0 in strikes


@pytest.mark.asyncio
async def test_scan_handles_exchange_error_gracefully():
    discovery = MarketDiscovery()
    discovery._server_ok = True

    fake_poly = MagicMock()
    fake_poly.fetch_markets.side_effect = RuntimeError("connection refused")
    fake_kalshi = MagicMock()
    fake_kalshi.fetch_markets.return_value = [
        _btc_market("kal-1", "BTC above $100,000?"),
    ]

    discovery._poly = fake_poly
    discovery._kalshi = fake_kalshi

    # Should not raise; just skip Polymarket and return Kalshi results
    contracts = await discovery.scan_btc_contracts()
    assert len(contracts) == 1
    assert contracts[0].venue == "kalshi"


@pytest.mark.asyncio
async def test_ensure_server_returns_false_on_error():
    discovery = MarketDiscovery()
    with patch("btc_pm_arb.feeds.discovery.MarketDiscovery._start_sidecar",
               side_effect=RuntimeError("no node")):
        ok = await discovery.ensure_server()
    assert ok is False
    assert discovery._server_ok is False
