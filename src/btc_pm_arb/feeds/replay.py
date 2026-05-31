"""Replay reader -- drives the agent off recorded frames (build step 5).

Paper-trading plan sections 3.5, 1.4; Fork 3.  This is the cold-path
counterpart to the live feed tasks: instead of three network feeds pushing
ticks, the reader opens the gzipped JSONL recordings written by
:mod:`btc_pm_arb.feeds.recorder`, replays each frame through the SAME parse
path the live feed uses (no second parser), advances the shared
:class:`~btc_pm_arb.clock.SimulatedClock` off the recorded outer ``ts``, and
finally fast-forwards the clock to expiry so the deterministic benchmark
settler fires (the recordings are two ~90 s windows that never reach a
contract expiry organically -- plan section 1.4).

Reuse, not re-implementation (guardrail: no second parse path)
--------------------------------------------------------------
* Deribit:    a feed instance is constructed (NOT connected) purely to reuse
              :meth:`DeribitFeed._handle_ticker`, the exact code the live
              message loop calls to turn a raw ticker payload into an
              ``OptionTick``.  The reader drains the feed's internal queue.
* Polymarket / Kalshi: the reader joins the recorded discovery (``/markets``)
              frames to the order-book (``/book`` resp. ``/orderbook``) frames
              by asset_id / token_id resp. ticker (Carried Gap b), then calls
              the feed module's ``_build_tick`` -> ``normalize_*_tick`` -- the
              same function the live poll loop calls.

Order-book enrichment (Polymarket)
----------------------------------
The live ``polymarket._build_tick`` collapses the ``/book`` response to a
single best bid/ask and does not carry depth onto the tick (``order_book_*``
stay empty), which the ``_reject_empty_book`` gate then rejects.  The
recordings preserve the FULL ``/book`` depth, so the reader re-attaches it to
``order_book_yes`` / ``order_book_no`` after the normalizer runs -- restoring
real recorded liquidity the live collapse discards, not loosening a gate.

Determinism (decision b -- see outputs/impl_replay_report.md)
-------------------------------------------------------------
Record timestamps stay on wall-clock and ``client_order_id`` stays a random
uuid4 (byte-identity would force a deterministic id, which ripples into the
live order-create / dedup path).  Determinism is asserted on the SEMANTIC
projection of the ledger -- sides, sizes, limits, edges, fill outcomes/prices,
settlement prices and P&L -- which a re-run reproduces exactly from the same
recorded frames.  The reader itself reaches NO network: it drives only the
in-memory pipeline + the deterministic benchmark settler.
"""

from __future__ import annotations

import gzip
import heapq
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import structlog

from btc_pm_arb.feeds import polymarket as _pm
from btc_pm_arb.feeds import kalshi as _kalshi
from btc_pm_arb.feeds.deribit import DeribitFeed
from btc_pm_arb.models import DataSource

if TYPE_CHECKING:
    from btc_pm_arb.main import Agent

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# Source replay priority for ties on the outer ``ts`` -- deterministic and
# arbitrary; only matters when two frames share an identical timestamp.
_SOURCE_ORDER: dict[str, int] = {"deribit": 0, "kalshi": 1, "polymarket": 2}


def _parse_ts(raw: str) -> datetime:
    """Parse a recorded ISO-8601 ``ts`` into a tz-aware UTC datetime."""
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ReplayReader:
    """Drive an :class:`~btc_pm_arb.main.Agent` off recorded frames.

    Usage::

        reader = ReplayReader(record_dir="data/recordings", date="2026-05-30",
                              agent=agent)
        await reader.run()        # ingest -> scan -> jump-to-expiry settle

    ``agent.clock`` MUST be a replay-mode ``SimulatedClock`` -- the reader is
    the only thing that advances it.
    """

    def __init__(
        self,
        *,
        record_dir: str | Path,
        date: str,
        agent: "Agent",
        sources: tuple[str, ...] = ("deribit", "kalshi", "polymarket"),
        hours: tuple[str, ...] | None = None,
        jump_to_expiry: bool = True,
    ) -> None:
        self._base = Path(record_dir)
        self._date = date
        self._agent = agent
        self._sources = sources
        self._hours = hours
        self._jump_to_expiry = jump_to_expiry
        if agent.clock.mode != "replay":
            raise ValueError(
                "ReplayReader requires an agent with a replay-mode clock; "
                f"got mode={agent.clock.mode!r}"
            )
        # Deribit feed instance reused purely for its _handle_ticker parse +
        # queue; never connected (no __aenter__ / _run_forever call).
        self._dfeed = DeribitFeed()
        self._dfeed._running = True
        # Per-venue discovery state: token_id/ticker -> market metadata dict,
        # joined to the book frames (Carried Gap b).
        self._pm_tracked: dict[str, dict] = {}
        self._kalshi_tracked: dict[str, dict] = {}
        # Counters surfaced for the smoke/diagnostics.
        self.stats: dict[str, int] = {
            "frames": 0,
            "deribit_ticks": 0,
            "pm_ticks": 0,
            "kalshi_ticks": 0,
            "settlements": 0,
        }

    # ── Frame source ──────────────────────────────────────────────────────────

    def _files_for(self, source: str) -> list[Path]:
        day_dir = self._base / source / self._date
        if not day_dir.is_dir():
            return []
        if self._hours is not None:
            paths = [day_dir / f"frames-{h}.jsonl.gz" for h in self._hours]
            return [p for p in paths if p.exists()]
        return sorted(day_dir.glob("frames-*.jsonl.gz"))

    def _source_lines(self, source: str) -> Iterator[tuple[datetime, int, str, dict]]:
        """Yield ``(ts, source_order, source, record)`` for one source, ts-sorted.

        Each recording file is already written in receive-time order, so a
        per-file sequential read is monotonic; concatenating the hour files
        of a day preserves that order.
        """
        order = _SOURCE_ORDER.get(source, 99)
        for path in self._files_for(source):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = _parse_ts(rec["ts"])
                    except (ValueError, KeyError):
                        continue
                    yield (ts, order, source, rec)

    def _merged_frames(self) -> Iterator[tuple[datetime, int, str, dict]]:
        """k-way merge the per-source streams by (ts, source_order)."""
        streams = [self._source_lines(s) for s in self._sources]
        # heapq.merge needs a key that orders tuples; (ts, order) is enough and
        # never compares the dict payload.
        return heapq.merge(*streams, key=lambda item: (item[0], item[1]))

    # ── Per-frame dispatch ────────────────────────────────────────────────────

    def _ingest_deribit(self, rec: dict) -> None:
        try:
            body = json.loads(rec["frame"])
        except (ValueError, KeyError):
            return
        if body.get("method") != "subscription":
            return  # rpc responses / heartbeats carry no tick
        data = body.get("params", {}).get("data", {})
        if not isinstance(data, dict):
            return
        # Reuse the exact live parse; it pushes any built OptionTick onto the
        # feed's queue.  Drain into the agent.
        self._dfeed._handle_ticker(data)
        while not self._dfeed._queue.empty():
            tick = self._dfeed._queue.get_nowait()
            self._agent.ingest_tick(tick)
            self.stats["deribit_ticks"] += 1

    def _ingest_polymarket(self, rec: dict, ts: datetime) -> None:
        endpoint = rec.get("endpoint") or ""
        try:
            body = json.loads(rec["frame"])
        except (ValueError, KeyError):
            return
        if endpoint.startswith("/markets"):
            rows = (
                body if isinstance(body, list)
                else (body.get("data") or body.get("markets") or [])
            )
            for market in rows:
                if not _pm._is_btc_binary_threshold(market):
                    continue
                token = _pm._resolve_yes_token(market)
                if token is not None:
                    self._pm_tracked[token] = market
            return
        if not endpoint.startswith("/book"):
            return
        token = endpoint.split("token_id=")[-1]
        meta = self._pm_tracked.get(token)
        if meta is None:
            return  # book frame for an untracked / non-BTC market (Carried Gap b)
        tick = _pm._build_tick(token, meta, body or {})
        if tick is None:
            return
        # Re-attach the FULL recorded /book depth the live collapse discards.
        tick.order_book_yes = _book_levels(body.get("asks"))
        tick.order_book_no = [
            (round(1.0 - price, 6), size)
            for price, size in _book_levels(body.get("bids"))
        ]
        # Stamp sim-time so the freshness gates and downstream records read the
        # recorded timeline, not wall-clock (the normalizer stamps utc_now()).
        tick.timestamp = ts
        self._agent.ingest_pm_tick(tick)
        self._agent.feed_health.record_tick(DataSource.POLYMARKET)
        self.stats["pm_ticks"] += 1

    def _ingest_kalshi(self, rec: dict, ts: datetime) -> None:
        endpoint = rec.get("endpoint") or ""
        try:
            body = json.loads(rec["frame"])
        except (ValueError, KeyError):
            return
        if endpoint.startswith("/markets") and "orderbook" not in endpoint:
            for market in (body.get("markets") or []):
                ticker = market.get("ticker")
                if ticker:
                    self._kalshi_tracked[ticker] = market
            return
        if "orderbook" not in endpoint:
            return
        # /markets/{ticker}/orderbook -> ticker is the path segment before it.
        ticker = endpoint.rstrip("/").split("/")[-2] if "/" in endpoint else ""
        meta = self._kalshi_tracked.get(ticker)
        if meta is None:
            return
        book = body.get("orderbook_fp") or {}
        tick = _kalshi._build_tick(ticker, meta, book)
        if tick is None:
            return
        tick.timestamp = ts
        self._agent.ingest_pm_tick(tick)
        self._agent.feed_health.record_tick(DataSource.KALSHI)
        self.stats["kalshi_ticks"] += 1

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self) -> dict[str, int]:
        """Replay every frame, run one scan, then jump-to-expiry settle.

        Returns the run :attr:`stats`.  Reaches no network: only the
        in-memory pipeline and the deterministic benchmark settler run.
        """
        clock = self._agent.clock
        for ts, _order, source, rec in self._merged_frames():
            # Advance the sim-clock onto the recorded timeline.  The clock may
            # be UNANCHORED (run() leaves replay clocks with no start so the
            # reader positions them from the first frame) -- clock.now() raises
            # until positioned, so the first frame advances unconditionally.
            # Thereafter the merged stream is monotonic; the >= guard tolerates
            # equal timestamps and never moves backwards.
            try:
                current = clock.now()
            except RuntimeError:
                current = None
            if current is None or ts > current:
                clock.advance_to(ts)
            self.stats["frames"] += 1
            if source == "deribit":
                self._ingest_deribit(rec)
            elif source == "polymarket":
                self._ingest_polymarket(rec, ts)
            elif source == "kalshi":
                self._ingest_kalshi(rec, ts)

        await self._scan_once()
        if self._jump_to_expiry:
            self._jump_to_expiry_and_settle()
        logger.info("replay.completed", **self.stats)
        return self.stats

    async def _scan_once(self) -> None:
        """Fold accumulated ticks into the surface/cache and run one scan.

        Mirrors the live ``_scan_task`` body (minus the dashboard push): build
        the cache from the Deribit surface, drain the PM-tick buffer through
        ``run_scan_pipeline`` (match -> edge -> filter -> place -> fill ->
        ledger), then mark open positions to market.
        """
        agent = self._agent
        dirty = agent.flush_ticks()
        if dirty:
            agent.update_cache_from_surface(dirty)
        pm_ticks = agent.flush_pm_ticks()
        await agent.run_scan_pipeline(pm_ticks)
        agent.paper_positions.mark_to_market(pm_ticks)

    def _jump_to_expiry_and_settle(self) -> None:
        """Fast-forward the sim-clock past every open position's expiry, settle.

        The recordings are two ~90 s windows that never reach a contract
        expiry (plan section 1.4); the benchmark settler is expiry-gated on the
        sim-clock, so without this jump it would never fire.  Advancing to the
        latest open expiry is a deterministic clock move -- the benchmark price
        (latest observed Deribit index) and the terminal-digital model are both
        deterministic, so settlement reproduces from the recorded surface with
        no live oracle call.
        """
        agent = self._agent
        open_positions = agent.paper_positions.open_positions()
        if not open_positions:
            return
        latest_expiry = max(p.expiry for p in open_positions)
        target = latest_expiry
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        if target > agent.clock.now():
            agent.clock.advance_to(target)
        self.stats["settlements"] = agent.paper_benchmark_settler.settle_due()


def _book_levels(raw_levels: object) -> list[tuple[float, float]]:
    """Parse a recorded /book side into ``[(price, size_usd), ...]`` ascending.

    Each level is ``{"price": "..", "size": ".."}`` (Polymarket CLOB shape).
    Sorted ascending by price so the YES ask-side walk lifts the cheapest
    level first (the convention the fill simulator / fill-adjusted-edge use).
    Size doubles as USD notional -- one YES share pays at most $1, so share
    count is the notional proxy the existing book-walk already assumes.
    """
    out: list[tuple[float, float]] = []
    if not isinstance(raw_levels, list):
        return out
    for level in raw_levels:
        try:
            price = float(level["price"])
            size = float(level.get("size") or level.get("size_usd") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        out.append((price, size))
    out.sort(key=lambda lvl: lvl[0])
    return out
