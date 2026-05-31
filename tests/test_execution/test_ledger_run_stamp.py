"""Tests for run_id + mode stamping on every PaperLedger append (build step 4).

The ledger stamps its run_id + mode onto each appended record (order, fill,
settlement, rejection) so live vs replay runs are separable + joinable.  A
bare PaperLedger(dir) stamps the field defaults ("", "live"), preserving
round-trip equality for callers that don't supply a run id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
    PaperRejectionRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.models import DataSource


_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=7)


def _order(cid: str = "co-1") -> PaperOrderRecord:
    return PaperOrderRecord(
        client_order_id=cid,
        signal_fingerprint="fp",
        created_at=_NOW,
        platform=DataSource.POLYMARKET,
        contract_id="PM-BTC-100000",
        side="yes",
        size_usd=200.0,
        limit_price=0.42,
        raw_edge=0.14,
        adjusted_edge=0.13,
        confidence=0.7,
        vol_regime="normal",
        feed_staleness_ms={},
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        expiry=_EXPIRY,
        strike=100_000.0,
        direction="above",
    )


def _fill(cid: str = "co-1") -> PaperFillRecord:
    return PaperFillRecord(
        client_order_id=cid,
        filled_at=_NOW,
        fill_price=0.42,
        fill_size_usd=200.0,
        fill_outcome="full",
        simulator_reason="book_walk_full",
    )


def _settlement(cid: str = "co-1") -> PaperSettlementRecord:
    return PaperSettlementRecord(
        client_order_id=cid,
        contract_id="PM-BTC-100000",
        platform=DataSource.POLYMARKET,
        side="yes",
        settled_at=_NOW + timedelta(days=8),
        settlement_price=1.0,
        payout_price=1.0,
        entry_price=0.42,
        size_usd=200.0,
        realized_pnl=116.0,
        outcome="win",
        theoretical_edge=0.13,
        expiry=_EXPIRY,
    )


def _rejection() -> PaperRejectionRecord:
    return PaperRejectionRecord(
        timestamp=_NOW,
        contract_id="PM-BTC-100000",
        platform=DataSource.POLYMARKET,
        reason_key="pm_spread",
        full_reason="pm_spread 0.20 > max 0.12",
        best_conservative_edge=0.005,
        vol_regime="normal",
    )


# -- Stamping ----------------------------------------------------------------


def test_ledger_stamps_run_id_and_mode_on_every_append(tmp_path):
    ledger = PaperLedger(tmp_path, run_id="run-xyz", mode="replay")
    ledger.append_order(_order())
    ledger.append_fill(_fill())
    ledger.append_settlement(_settlement())
    ledger.append_rejection(_rejection())

    order = list(ledger.replay_orders())[0]
    fill = list(ledger.replay_fills())[0]
    settle = list(ledger.replay_settlements())[0]
    reject = list(ledger.replay_rejections())[0]

    for rec in (order, fill, settle, reject):
        assert rec.run_id == "run-xyz"
        assert rec.mode == "replay"


def test_stamp_overrides_record_field_value(tmp_path):
    """The ledger's run_id wins even if the record carries one -- the
    stamp is authoritative ('every append carries the run's id')."""
    ledger = PaperLedger(tmp_path, run_id="ledger-run", mode="live")
    rec = _order().model_copy(update={"run_id": "stale-run", "mode": "replay"})
    ledger.append_order(rec)
    loaded = list(ledger.replay_orders())[0]
    assert loaded.run_id == "ledger-run"
    assert loaded.mode == "live"


def test_default_ledger_stamps_defaults_and_round_trips(tmp_path):
    """A bare PaperLedger(dir) stamps the field defaults, so a record
    constructed without run fields round-trips equal."""
    ledger = PaperLedger(tmp_path)
    rec = _fill()
    ledger.append_fill(rec)
    loaded = list(ledger.replay_fills())[0]
    assert loaded.run_id == ""
    assert loaded.mode == "live"
    assert loaded == rec        # default stamp == record defaults


def test_caller_record_object_not_mutated_by_stamp(tmp_path):
    """Stamping copies; the caller's in-memory record is untouched."""
    ledger = PaperLedger(tmp_path, run_id="run-1", mode="replay")
    rec = _order()
    ledger.append_order(rec)
    assert rec.run_id == ""      # original unchanged
    assert rec.mode == "live"


def test_records_carry_run_fields_with_schema_version_1(tmp_path):
    """run_id/mode are additive: schema_version stays 1 so existing readers
    (analyze_paper_ledger) still accept the records."""
    rec = _order()
    assert rec.schema_version == 1
    assert rec.run_id == ""
    assert rec.mode == "live"
