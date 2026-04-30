"""Market discovery via PMXT — finds BTC price threshold contracts on Polymarket and Kalshi.

PMXT is used *only* for discovery (finding new contracts, refreshing the list of
active markets).  All real-time pricing and execution use our direct API clients.

The module is designed to be resilient:
  * If the PMXT sidecar is unavailable, ``scan_btc_contracts()`` returns an
    empty list and logs a warning — the agent continues with whatever contracts
    it already knows about.
  * Discovery runs on a slow 5-minute loop; it is not latency-sensitive.

PMXT sidecar lifecycle:
  * ``ensure_server()`` calls ``pmxt.server_manager.ServerManager.ensure_server_running()``
    which starts the Node.js sidecar if not already running.
  * ``health_check()`` verifies the sidecar is responsive.
  * The sidecar is started once at agent boot and monitored by a background task.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# BTC price threshold patterns (matches "$100k", "$100,000", "100000", "100K")
_STRIKE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\$\s*([\d,]+)\s*[kK]", re.IGNORECASE),          # $100k, $100K
    re.compile(r"\$\s*([\d,]+)"),                                  # $100,000
    re.compile(r"\b(?:BTC|Bitcoin)\b.*?\b(\d{4,6})\b", re.IGNORECASE),  # BTC/Bitcoin ... 95000
]

_BTC_KEYWORDS = {"btc", "bitcoin", "above", "below", "price", "reach", "exceed"}


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class DiscoveredContract:
    """Normalized BTC price threshold contract from PMXT discovery."""

    venue: str                  # "polymarket" | "kalshi"
    market_id: str              # PMXT market_id
    outcome_id: str             # venue-specific trading ID (token_id / ticker)
    title: str
    strike_price: float         # USD threshold
    expiry: datetime | None
    yes_price: float            # current YES probability [0, 1]
    volume_24h: float
    liquidity: float
    url: str
    raw: dict = field(default_factory=dict, repr=False)


# ── Strike extractor ───────────────────────────────────────────────────────────

def extract_strike(title: str, description: str = "") -> float | None:
    """Parse a USD strike price from a market title or description."""
    for text in (title, description or ""):
        for pat in _STRIKE_PATTERNS:
            m = pat.search(text)
            if m:
                raw = m.group(1).replace(",", "")
                try:
                    val = float(raw)
                    # 'k' suffix means thousands
                    if re.search(r"[kK]", m.group(0)):
                        val *= 1_000
                    # Sanity: BTC strikes are between $1k and $10M
                    if 1_000 <= val <= 10_000_000:
                        return val
                except ValueError:
                    continue
    return None


def _is_btc_market(title: str, description: str = "") -> bool:
    combined = (title + " " + (description or "")).lower()
    return any(kw in combined for kw in _BTC_KEYWORDS) and (
        "btc" in combined or "bitcoin" in combined
    )


# ── Market discovery ───────────────────────────────────────────────────────────

class MarketDiscovery:
    """Discover active BTC price threshold contracts using PMXT.

    Usage::

        discovery = MarketDiscovery()
        await discovery.ensure_server()
        contracts = await discovery.scan_btc_contracts()
    """

    def __init__(self) -> None:
        self._poly: Any = None
        self._kalshi: Any = None
        self._server_ok: bool = False

    async def ensure_server(self) -> bool:
        """Start the PMXT sidecar if not running.  Returns True on success."""
        try:
            import pmxt
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._start_sidecar)
            self._poly = pmxt.Polymarket()
            self._kalshi = pmxt.Kalshi()
            self._server_ok = True
            logger.info("pmxt.sidecar_ready")
            return True
        except Exception as exc:
            logger.warning("pmxt.sidecar_unavailable", error=str(exc))
            self._server_ok = False
            return False

    def _start_sidecar(self) -> None:
        import pmxt
        mgr = pmxt.server_manager.ServerManager()
        mgr.ensure_server_running()

    async def health_check(self) -> bool:
        """Return True if the PMXT sidecar is responsive."""
        if not self._server_ok:
            return False
        try:
            loop = asyncio.get_running_loop()
            # A lightweight call to verify the sidecar is alive
            await loop.run_in_executor(
                None,
                lambda: pmxt.server_manager.ServerManager().is_server_running()
                if hasattr(pmxt.server_manager.ServerManager(), "is_server_running")
                else True,
            )
            return True
        except Exception:
            self._server_ok = False
            return False

    async def scan_btc_contracts(self) -> list[DiscoveredContract]:
        """Search both Polymarket and Kalshi for BTC price threshold markets.

        Falls back to empty list if PMXT is unavailable.
        """
        if not self._server_ok:
            logger.debug("pmxt.discovery_skipped_server_not_ready")
            return []

        loop = asyncio.get_running_loop()
        results: list[DiscoveredContract] = []

        for exchange_name, exchange in [("polymarket", self._poly), ("kalshi", self._kalshi)]:
            if exchange is None:
                continue
            try:
                markets = await loop.run_in_executor(
                    None,
                    lambda ex=exchange: ex.fetch_markets({"query": "BTC bitcoin price"}),
                )
                for market in markets:
                    contracts = self._parse_market(market, exchange_name)
                    results.extend(contracts)
                logger.debug(
                    "pmxt.discovery_scan",
                    venue=exchange_name,
                    total_markets=len(markets),
                    btc_contracts=sum(1 for m in markets if _is_btc_market(m.title)),
                )
            except Exception as exc:
                logger.warning("pmxt.discovery_error", venue=exchange_name, error=str(exc))

        logger.info("pmxt.discovered", total=len(results))
        return results

    def _parse_market(self, market: Any, venue: str) -> list[DiscoveredContract]:
        """Convert a PMXT UnifiedMarket to DiscoveredContract(s)."""
        if not _is_btc_market(market.title, market.description or ""):
            return []

        strike = extract_strike(market.title, market.description or "")
        if strike is None:
            return []

        contracts: list[DiscoveredContract] = []
        for outcome in market.outcomes:
            # Only take YES outcomes for binary BTC-above contracts
            if outcome.label.lower() not in {"yes", "above", "up", "higher"}:
                continue
            contracts.append(DiscoveredContract(
                venue=venue,
                market_id=market.market_id,
                outcome_id=outcome.outcome_id,
                title=market.title,
                strike_price=strike,
                expiry=market.resolution_date,
                yes_price=outcome.price,
                volume_24h=market.volume_24h,
                liquidity=market.liquidity,
                url=market.url,
                raw={
                    "market_id": market.market_id,
                    "outcome_id": outcome.outcome_id,
                },
            ))
        return contracts


# ── Background discovery task ──────────────────────────────────────────────────

async def run_discovery_loop(
    discovery: MarketDiscovery,
    on_contracts: "asyncio.Queue[list[DiscoveredContract]]",
    stop_event: asyncio.Event,
    interval_secs: float = 300.0,  # 5 minutes
) -> None:
    """Background task: periodically scan for new BTC contracts.

    Discovered contracts are pushed into ``on_contracts`` queue for the
    main pipeline to consume.
    """
    logger.info("discovery_loop.starting", interval_secs=interval_secs)
    # Initial server start
    await discovery.ensure_server()

    while not stop_event.is_set():
        if not await discovery.health_check():
            logger.warning("discovery_loop.sidecar_down_retrying")
            await discovery.ensure_server()

        contracts = await discovery.scan_btc_contracts()
        if contracts:
            await on_contracts.put(contracts)

        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()),
                timeout=interval_secs,
            )
        except asyncio.TimeoutError:
            pass

    logger.info("discovery_loop.stopped")
