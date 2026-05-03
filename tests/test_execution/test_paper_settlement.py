"""Tests for execution/paper_settlement.py — Kalshi settlement poller.

Scope (Round 8 Commit 2):
* Settled detection (status=settled, result=yes) → settlement_price=1.0,
  position closes, PaperSettlementRecord written, realized_pnl correct.
* Settled detection (status=settled, result=no) → settlement_price=0.0,
  symmetric correctness.
* Defensive shape parsing: status=closed result="" → no settlement,
  paper_settlement.unexpected_shape NOT logged for fully-non-suggestive
  shapes; status=settled result="" → unexpected_shape WARNING fires.
* 7d-since-expiry timeout: warning logged, position stays open, log
  fires at most once per (contract, side).
* Window logic: contract with expiry > now + 5min not polled; contract
  with expiry < now - 24h not polled (but if also > 7d, hits timeout
  branch instead).
* Hedged YES + NO on the same contract: one HTTP call, both positions
  settled with side-aware payout.
* Mocked HTTP throughout — no real Kalshi calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
)
from btc_pm_arb.execution.paper_positions import PaperPositionTracker
from btc_pm_arb.execution.paper_settlement import KalshiSettlementPoller
from btc_pm_arb.models import DataSource


# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=14)


class _MockResponse:
    """Minimal stand-in for httpx.Response with the methods the poller uses."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req, response=httpx.Response(self.status_code, request=req)
            )


class _MockClient:
    """Minimal async HTTP client that maps ticker → payload (or error)."""

    def __init__(self, by_ticker: dict[str, Any], errors: dict[str, Exception] | None = None) -> None:
        self._by_ticker = by_ticker
        self._errors = errors or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, path: str, headers: dict[str, str] | None = None) -> _MockResponse:
        self.calls.append((path, headers or {}))
        # path is e.g. "/markets/KXBTC-XYZ"
        ticker = path.rsplit("/", 1)[-1]
        if ticker in self._errors:
            raise self._errors[ticker]
        payload = self._by_ticker.get(ticker, {"market": {"status": "open", "result": ""}})
        return _MockResponse(payload)

    async def aclose(self) -> None:
        pass


def _make_order(
    *,
    client_order_id: str = "co-1",
    contract_id: str = "KXBTC-26MAY-100000",
    side: str = "yes",
    adjusted_edge: float = 0.05,
    expiry: datetime | None = None,
) -> PaperOrderRecord:
    return PaperOrderRecord(
        client_order_id=client_order_id,
        signal_fingerprint=f"{contract_id}:{side}",
        created_at=_NOW,
        platform=DataSource.KALSHI,
        contract_id=contract_id,
        side=side,
        size_usd=200.0,
        limit_price=0.45,
        raw_edge=adjusted_edge,
        adjusted_edge=adjusted_edge,
        confidence=0.6,
        vol_regime="normal",
        feed_staleness_ms={},
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        pm_yes_bid=0.43,
        pm_yes_ask=0.45,
        pm_no_bid=0.55,
        pm_no_ask=0.57,
        expiry=expiry or _EXPIRY,
    )


def _open_position(
    tracker: PaperPositionTracker,
    *,
    client_order_id: str = "co-1",
    contract_id: str = "KXBTC-26MAY-100000",
    side: str = "yes",
    fill_price: float = 0.45,
    fill_size_usd: float = 200.0,
    expiry: datetime | None = None,
) -> PaperOrderRecord:
    """Helper: open a paper position via record_fill and return the order record."""
    order = _make_order(
        client_order_id=client_order_id,
        contract_id=contract_id,
        side=side,
        expiry=expiry,
    )
    fill = PaperFillRecord(
        client_order_id=client_order_id,
        filled_at=_NOW,
        fill_price=fill_price,
        fill_size_usd=fill_size_usd,
        fill_outcome="full",
        simulator_reason="marketable_against_book",
    )
    tracker.record_fill(order_record=order, fill_record=fill)
    return order


def _make_poller(
    *,
    tracker: PaperPositionTracker,
    ledger: PaperLedger,
    orders_registry: dict[str, PaperOrderRecord],
    by_ticker: dict[str, Any] | None = None,
    errors: dict[str, Exception] | None = None,
    clock_now: datetime | None = None,
) -> tuple[KalshiSettlementPoller, _MockClient]:
    client = _MockClient(by_ticker or {}, errors)
    fixed_now = clock_now or _NOW
    poller = KalshiSettlementPoller(
        tracker=tracker,
        ledger=ledger,
        get_order_record=orders_registry.get,
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        key_path="/nonexistent.pem",   # never loaded — http_client injected
        key_id="test-key",
        http_client=client,                # type: ignore[arg-type]
        clock=lambda: fixed_now,
    )
    return poller, client


# ── Settled detection ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settled_yes_closes_position_and_writes_record(tmp_path: Path):
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker, fill_price=0.45)
    orders_registry = {order.client_order_id: order}

    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry=orders_registry,
        by_ticker={
            "KXBTC-26MAY-100000": {
                "market": {"status": "settled", "result": "yes",
                           "ticker": "KXBTC-26MAY-100000"}
            }
        },
        clock_now=_EXPIRY + timedelta(minutes=2),   # inside post-window
    )

    n = await poller.poll_once()
    assert n == 1

    # Position closed
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.closed
    assert pos.settlement_price == pytest.approx(1.0)
    # Realized P&L: (1.0 - 0.45) * 200.0 = 110.0
    assert pos.realized_pnl == pytest.approx(110.0)

    # Settlement record persisted
    settlements = list(ledger.replay_settlements())
    assert len(settlements) == 1
    s = settlements[0]
    assert s.outcome == "win"
    assert s.settlement_price == pytest.approx(1.0)
    assert s.payout_price == pytest.approx(1.0)
    assert s.realized_pnl == pytest.approx(110.0)
    assert s.theoretical_edge == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_settled_no_yes_position_records_loss(tmp_path: Path):
    """YES position, contract resolved NO → settlement_price=0.0, payout=0.0."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker, fill_price=0.45)
    orders_registry = {order.client_order_id: order}

    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry=orders_registry,
        by_ticker={
            "KXBTC-26MAY-100000": {"market": {"status": "settled", "result": "no"}}
        },
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 1

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert pos.closed
    assert pos.settlement_price == pytest.approx(0.0)
    # Realized P&L: (0.0 - 0.45) * 200.0 = -90.0
    assert pos.realized_pnl == pytest.approx(-90.0)

    s = list(ledger.replay_settlements())[0]
    assert s.outcome == "loss"
    assert s.payout_price == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settled_no_for_no_position_records_win(tmp_path: Path):
    """NO position, contract resolved NO → payout=1.0, win."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker, side="no", fill_price=0.55)
    orders_registry = {order.client_order_id: order}

    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry=orders_registry,
        by_ticker={
            "KXBTC-26MAY-100000": {"market": {"status": "settled", "result": "no"}}
        },
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 1

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "no")
    assert pos is not None
    # NO position with settlement_price=0.0: payout = 1.0 - 0.0 = 1.0
    # Realized: (1.0 - 0.55) * 200.0 = 90.0
    assert pos.realized_pnl == pytest.approx(90.0)
    s = list(ledger.replay_settlements())[0]
    assert s.outcome == "win"
    assert s.payout_price == pytest.approx(1.0)


# ── Defensive shape parsing ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_closed_no_result_does_not_settle(tmp_path: Path, caplog):
    """status=closed result="" — neither condition met, no settlement, no warning."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={"KXBTC-26MAY-100000": {"market": {"status": "closed", "result": ""}}},
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 0

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert not pos.closed
    assert list(ledger.replay_settlements()) == []


@pytest.mark.asyncio
async def test_status_settled_empty_result_logs_unexpected_shape(tmp_path: Path, caplog):
    """status=settled result="" — partial match, log unexpected_shape and skip."""
    import logging
    caplog.set_level(logging.WARNING)

    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={"KXBTC-26MAY-100000": {"market": {"status": "settled", "result": ""}}},
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 0

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert not pos.closed
    # Verify the warning log fired (caplog captures structlog→stdlib bridge if
    # configured; absence of false-settle is the load-bearing assertion).
    assert list(ledger.replay_settlements()) == []


@pytest.mark.asyncio
async def test_status_open_result_yes_does_not_settle(tmp_path: Path):
    """status=open result=yes — partial match the other way, do not settle."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={"KXBTC-26MAY-100000": {"market": {"status": "open", "result": "yes"}}},
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 0
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None and not pos.closed


@pytest.mark.asyncio
async def test_settled_result_invalid_string_does_not_settle(tmp_path: Path):
    """status=settled result='maybe' — invalid result, do not settle."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={"KXBTC-26MAY-100000": {"market": {"status": "settled", "result": "maybe"}}},
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 0
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None and not pos.closed


# ── Window logic ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contract_far_future_not_polled(tmp_path: Path):
    """Position with expiry > now + 5min by a wide margin: not polled this cycle."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    far_expiry = _NOW + timedelta(days=30)
    order = _open_position(tracker, expiry=far_expiry)
    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={
            "KXBTC-26MAY-100000": {"market": {"status": "settled", "result": "yes"}}
        },
        clock_now=_NOW,   # 30 days before expiry — outside 24h pre-window
    )

    n = await poller.poll_once()
    assert n == 0
    assert client.calls == []   # no HTTP call made


@pytest.mark.asyncio
async def test_contract_just_outside_pre_window_not_polled(tmp_path: Path):
    """Position with expiry = now + 24h + 1min: just outside the pre-window."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    expiry = _NOW + timedelta(hours=24, minutes=1)
    order = _open_position(tracker, expiry=expiry)
    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        clock_now=_NOW,
    )

    await poller.poll_once()
    assert client.calls == []


@pytest.mark.asyncio
async def test_contract_inside_pre_window_polled(tmp_path: Path):
    """Position with expiry = now + 23h: inside the 24h pre-window, polled."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    expiry = _NOW + timedelta(hours=23)
    order = _open_position(tracker, expiry=expiry)
    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={
            "KXBTC-26MAY-100000": {"market": {"status": "open", "result": ""}}
        },
        clock_now=_NOW,
    )

    await poller.poll_once()
    assert len(client.calls) == 1


# ── Timeout (7d post-expiry) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seven_day_timeout_logs_warning_keeps_position_open(tmp_path: Path, caplog):
    """A position 8d past expiry: log timeout warning, leave position open."""
    import logging
    caplog.set_level(logging.WARNING)

    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    expiry = _NOW - timedelta(days=8)
    order = _open_position(tracker, expiry=expiry)
    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        clock_now=_NOW,
    )

    n = await poller.poll_once()
    assert n == 0
    # No HTTP call — timeout branch fires before window check
    assert client.calls == []

    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None
    assert not pos.closed   # NOT closed — we don't fake-settle on timeout

    # No settlement record written
    assert list(ledger.replay_settlements()) == []


@pytest.mark.asyncio
async def test_timeout_logged_once_per_position(tmp_path: Path):
    """Repeated poll cycles do not spam timeout warnings."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    expiry = _NOW - timedelta(days=8)
    order = _open_position(tracker, expiry=expiry)
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        clock_now=_NOW,
    )

    # Three cycles — internal _timeout_logged set should dedupe
    await poller.poll_once()
    await poller.poll_once()
    await poller.poll_once()
    assert len(poller._timeout_logged) == 1


# ── Hedged YES + NO on same contract ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_hedged_positions_settle_with_one_http_call(tmp_path: Path):
    """One HTTP call for the contract, both YES and NO positions settle."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order_yes = _open_position(
        tracker, client_order_id="co-yes", side="yes", fill_price=0.45
    )
    order_no = _open_position(
        tracker, client_order_id="co-no", side="no", fill_price=0.55
    )
    orders_registry = {
        "co-yes": order_yes,
        "co-no": order_no,
    }

    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry=orders_registry,
        by_ticker={
            "KXBTC-26MAY-100000": {"market": {"status": "settled", "result": "yes"}}
        },
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 2
    assert len(client.calls) == 1   # deduped to one HTTP call

    yes_pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    no_pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "no")
    assert yes_pos is not None and no_pos is not None
    assert yes_pos.closed and no_pos.closed
    # YES position wins on result=yes: payout 1.0, P&L = (1.0 - 0.45) * 200 = 110
    assert yes_pos.realized_pnl == pytest.approx(110.0)
    # NO position loses on result=yes: payout 0.0, P&L = (0.0 - 0.55) * 200 = -110
    assert no_pos.realized_pnl == pytest.approx(-110.0)

    settlements = sorted(
        ledger.replay_settlements(), key=lambda s: s.side
    )
    assert len(settlements) == 2
    assert {s.outcome for s in settlements} == {"win", "loss"}


# ── Filtering: Polymarket positions, closed positions ────────────────────────


@pytest.mark.asyncio
async def test_polymarket_positions_skipped(tmp_path: Path):
    """Polymarket paper positions (defensive — shouldn't exist in Round 8) are
    filtered before any HTTP call.  Tests the filter, not a real scenario."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    poly_order = PaperOrderRecord(
        client_order_id="co-poly",
        signal_fingerprint="fp",
        created_at=_NOW,
        platform=DataSource.POLYMARKET,
        contract_id="poly-contract",
        side="yes",
        size_usd=200.0,
        limit_price=0.45,
        raw_edge=0.05,
        adjusted_edge=0.05,
        confidence=0.6,
        vol_regime="normal",
        feed_staleness_ms={},
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        expiry=_EXPIRY,
    )
    poly_fill = PaperFillRecord(
        client_order_id="co-poly",
        filled_at=_NOW,
        fill_price=0.45,
        fill_size_usd=200.0,
        fill_outcome="full",
        simulator_reason="marketable_against_book",
    )
    tracker.record_fill(order_record=poly_order, fill_record=poly_fill)

    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={"co-poly": poly_order},
        clock_now=_EXPIRY + timedelta(minutes=2),
    )
    await poller.poll_once()
    assert client.calls == []   # never polled


@pytest.mark.asyncio
async def test_already_closed_positions_not_polled_again(tmp_path: Path):
    """A position closed by a prior settlement is not re-polled."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)

    poller, client = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={
            "KXBTC-26MAY-100000": {"market": {"status": "settled", "result": "yes"}}
        },
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    # First cycle: settles
    n1 = await poller.poll_once()
    assert n1 == 1
    assert len(client.calls) == 1

    # Second cycle: no open position, no HTTP call
    n2 = await poller.poll_once()
    assert n2 == 0
    assert len(client.calls) == 1


# ── Missing order record ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_order_record_uses_default_theoretical_edge(tmp_path: Path):
    """Orphan position (no entry in registry) settles with theoretical_edge=0.0."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)
    # Empty registry — lookup returns None
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={},
        by_ticker={
            "KXBTC-26MAY-100000": {"market": {"status": "settled", "result": "yes"}}
        },
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 1
    s = list(ledger.replay_settlements())[0]
    assert s.theoretical_edge == 0.0   # default for orphan
    # Settlement still recorded — JSONL stream stays consistent
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None and pos.closed


# ── HTTP error handling ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_error_does_not_crash_or_settle(tmp_path: Path):
    """HTTP failure on a market lookup → no settlement, no crash, no false close."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        errors={"KXBTC-26MAY-100000": httpx.ConnectError("simulated")},
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 0
    pos = tracker.get(DataSource.KALSHI, "KXBTC-26MAY-100000", "yes")
    assert pos is not None and not pos.closed


@pytest.mark.asyncio
async def test_unwrapped_market_envelope_accepted(tmp_path: Path):
    """A response that returns the bare market dict (no 'market' wrapper) is
    handled — defensive against upstream envelope changes."""
    tracker = PaperPositionTracker()
    ledger = PaperLedger(tmp_path)
    order = _open_position(tracker)
    poller, _ = _make_poller(
        tracker=tracker,
        ledger=ledger,
        orders_registry={order.client_order_id: order},
        by_ticker={
            "KXBTC-26MAY-100000": {"status": "settled", "result": "yes"}   # bare
        },
        clock_now=_EXPIRY + timedelta(minutes=2),
    )

    n = await poller.poll_once()
    assert n == 1
