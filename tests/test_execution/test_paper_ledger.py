"""Tests for execution/paper_ledger.py — JSONL writer/reader and record schemas.

Scope (Round 8 Commit 1):
* Schema round-trip: each record type writes and reads back equal to the original.
* BookLevel sub-model serialises as named-field dicts (forward-compat with raw json.loads).
* schema_version is present on all records and defaults to 1.
* Append-only: a fresh PaperLedger pointing at the same dir reads previous lines.
* Skip-and-warn-with-counter: malformed trailing line and mid-file malformed line
  both increment n_parse_errors; surrounding records still load.
* health() surfaces the right counters and paths.

Settlement-replay invariant test lives in test_paper_positions.py (it's
load-bearing for the position tracker, not the ledger).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from btc_pm_arb.execution.paper_ledger import (
    BookLevel,
    PaperFillRecord,
    PaperLedger,
    PaperOrderRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.models import DataSource


# ── Fixtures / helpers ────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=14)


def _make_order(
    *,
    client_order_id: str = "co-1",
    side: str = "yes",
    contract_id: str = "KXBTC-26MAY01-100000",
) -> PaperOrderRecord:
    return PaperOrderRecord(
        client_order_id=client_order_id,
        signal_fingerprint=f"{contract_id}:buy_yes:{_EXPIRY.isoformat()}",
        created_at=_NOW,
        platform=DataSource.KALSHI,
        contract_id=contract_id,
        side=side,
        size_usd=200.0,
        limit_price=0.45,
        raw_edge=0.05,
        adjusted_edge=0.045,
        fill_adjusted_edge=0.043,
        confidence=0.62,
        vol_regime="normal",
        feed_staleness_ms={"deribit": 12.5, "kalshi": 3500.0},
        strike_gap_pct=0.005,
        expiry_gap_hours=2.5,
        match_quality=0.87,
        pm_yes_bid=0.43,
        pm_yes_ask=0.45,
        pm_no_bid=0.55,
        pm_no_ask=0.57,
        order_book_yes=[
            BookLevel(price=0.43, size_usd=500.0),
            BookLevel(price=0.42, size_usd=1000.0),
        ],
        order_book_no=[BookLevel(price=0.55, size_usd=300.0)],
        expiry=_EXPIRY,
        dry_run=True,
    )


def _make_fill(
    *,
    client_order_id: str = "co-1",
    outcome: str = "full",
    fill_price: float | None = 0.45,
    fill_size_usd: float = 200.0,
    reason: str = "marketable_against_book",
) -> PaperFillRecord:
    return PaperFillRecord(
        client_order_id=client_order_id,
        filled_at=_NOW,
        fill_price=fill_price,
        fill_size_usd=fill_size_usd,
        fill_outcome=outcome,
        simulator_reason=reason,
        fees_usd=0.0,
    )


def _make_settlement(
    *,
    client_order_id: str = "co-1",
    contract_id: str = "KXBTC-26MAY01-100000",
    side: str = "yes",
    settlement_price: float = 1.0,
) -> PaperSettlementRecord:
    payout = settlement_price if side == "yes" else 1.0 - settlement_price
    realized = (payout - 0.45) * 200.0
    if realized > 1e-4:
        outcome = "win"
    elif realized < -1e-4:
        outcome = "loss"
    else:
        outcome = "push"
    return PaperSettlementRecord(
        client_order_id=client_order_id,
        contract_id=contract_id,
        platform=DataSource.KALSHI,
        side=side,
        settled_at=_NOW + timedelta(days=14),
        settlement_price=settlement_price,
        payout_price=payout,
        entry_price=0.45,
        size_usd=200.0,
        realized_pnl=realized,
        fees_usd=0.0,
        outcome=outcome,
        theoretical_edge=0.045,
        expiry=_EXPIRY,
    )


# ── Schema round-trip ─────────────────────────────────────────────────────────


def test_paper_order_record_round_trip(tmp_path: Path):
    ledger = PaperLedger(tmp_path)
    original = _make_order()
    ledger.append_order(original)

    loaded = list(ledger.replay_orders())
    assert len(loaded) == 1
    assert loaded[0] == original


def test_paper_fill_record_round_trip(tmp_path: Path):
    ledger = PaperLedger(tmp_path)
    original = _make_fill()
    ledger.append_fill(original)

    loaded = list(ledger.replay_fills())
    assert len(loaded) == 1
    assert loaded[0] == original


def test_paper_fill_record_no_fill_round_trip(tmp_path: Path):
    """``no_fill`` outcomes have ``fill_price=None`` and ``fill_size_usd=0``."""
    ledger = PaperLedger(tmp_path)
    original = _make_fill(
        outcome="no_fill",
        fill_price=None,
        fill_size_usd=0.0,
        reason="non_marketable_dropped",
    )
    ledger.append_fill(original)

    loaded = list(ledger.replay_fills())
    assert len(loaded) == 1
    assert loaded[0] == original
    assert loaded[0].fill_price is None
    assert loaded[0].fill_size_usd == 0.0


def test_paper_settlement_record_round_trip(tmp_path: Path):
    ledger = PaperLedger(tmp_path)
    original = _make_settlement()
    ledger.append_settlement(original)

    loaded = list(ledger.replay_settlements())
    assert len(loaded) == 1
    assert loaded[0] == original


# ── BookLevel serialisation shape ─────────────────────────────────────────────


def test_book_level_serialises_as_named_field_dict(tmp_path: Path):
    """Round 9 readers using raw ``json.loads`` must see ``{"price": ..., "size_usd": ...}``,
    not positional arrays.  Defends against a future field reorder.
    """
    ledger = PaperLedger(tmp_path)
    ledger.append_order(_make_order())

    # Read the raw JSON line — bypass pydantic to confirm the wire format.
    raw_line = (tmp_path / "orders.jsonl").read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(raw_line)

    assert isinstance(payload["order_book_yes"], list)
    assert payload["order_book_yes"][0] == {"price": 0.43, "size_usd": 500.0}
    assert payload["order_book_yes"][1] == {"price": 0.42, "size_usd": 1000.0}
    # No tuples / arrays in the level dicts:
    assert isinstance(payload["order_book_yes"][0]["price"], float)
    assert isinstance(payload["order_book_yes"][0]["size_usd"], float)


# ── schema_version present on every record ────────────────────────────────────


def test_schema_version_present_and_defaults_to_one(tmp_path: Path):
    """All three record kinds carry ``schema_version: int = 1`` in the wire format."""
    ledger = PaperLedger(tmp_path)
    ledger.append_order(_make_order())
    ledger.append_fill(_make_fill())
    ledger.append_settlement(_make_settlement())

    for path in [
        tmp_path / "orders.jsonl",
        tmp_path / "fills.jsonl",
        tmp_path / "settlements.jsonl",
    ]:
        line = path.read_text(encoding="utf-8").splitlines()[0]
        payload = json.loads(line)
        assert "schema_version" in payload, f"missing in {path.name}"
        assert payload["schema_version"] == 1, f"wrong default in {path.name}"
        # ``kind`` discriminator must also be present
        assert "kind" in payload
        assert payload["kind"] in {"order", "fill", "settlement"}


# ── Append-only across instances ──────────────────────────────────────────────


def test_append_only_visible_to_fresh_ledger(tmp_path: Path):
    """A second ``PaperLedger`` against the same dir reads previously-written records."""
    ledger_a = PaperLedger(tmp_path)
    ledger_a.append_order(_make_order(client_order_id="co-1"))
    ledger_a.append_order(_make_order(client_order_id="co-2"))

    ledger_b = PaperLedger(tmp_path)
    loaded = list(ledger_b.replay_orders())
    assert [o.client_order_id for o in loaded] == ["co-1", "co-2"]


def test_append_does_not_truncate_existing(tmp_path: Path):
    """A second writer appending to the same file does not truncate prior content."""
    ledger_a = PaperLedger(tmp_path)
    ledger_a.append_order(_make_order(client_order_id="co-1"))

    ledger_b = PaperLedger(tmp_path)
    ledger_b.append_order(_make_order(client_order_id="co-2"))

    # Re-read with a third instance to be unambiguous about state.
    ledger_c = PaperLedger(tmp_path)
    loaded = list(ledger_c.replay_orders())
    assert [o.client_order_id for o in loaded] == ["co-1", "co-2"]


# ── Skip-and-warn-with-counter reader policy ──────────────────────────────────


def test_truncated_trailing_line_skipped_with_warning(tmp_path: Path):
    """A truncated last line increments n_parse_errors and surrounding records load."""
    ledger = PaperLedger(tmp_path)
    ledger.append_order(_make_order(client_order_id="co-1"))
    ledger.append_order(_make_order(client_order_id="co-2"))

    # Manually append a truncated JSON fragment (no closing brace, no newline).
    with open(tmp_path / "orders.jsonl", "a", encoding="utf-8") as f:
        f.write('{"kind":"order","schema_version":1,"client_order_id":"co-3"')

    fresh = PaperLedger(tmp_path)
    loaded = list(fresh.replay_orders())
    assert [o.client_order_id for o in loaded] == ["co-1", "co-2"]
    assert fresh.health()["n_parse_errors"] == 1
    assert fresh.health()["n_records_loaded"] == 2


def test_mid_file_malformed_line_skipped(tmp_path: Path):
    """A malformed line in the middle of the file is skipped; surrounding records load."""
    ledger = PaperLedger(tmp_path)
    ledger.append_order(_make_order(client_order_id="co-1"))

    # Inject a malformed JSON line by hand.
    with open(tmp_path / "orders.jsonl", "a", encoding="utf-8") as f:
        f.write("{not-valid-json-at-all}\n")

    ledger.append_order(_make_order(client_order_id="co-2"))

    fresh = PaperLedger(tmp_path)
    loaded = list(fresh.replay_orders())
    assert [o.client_order_id for o in loaded] == ["co-1", "co-2"]
    assert fresh.health()["n_parse_errors"] == 1
    assert fresh.health()["n_records_loaded"] == 2


def test_blank_lines_silently_skipped(tmp_path: Path):
    """Blank lines (e.g. from abrupt truncation) are not parse errors."""
    ledger = PaperLedger(tmp_path)
    ledger.append_order(_make_order(client_order_id="co-1"))

    with open(tmp_path / "orders.jsonl", "a", encoding="utf-8") as f:
        f.write("\n\n")   # Two blank lines

    ledger.append_order(_make_order(client_order_id="co-2"))

    fresh = PaperLedger(tmp_path)
    loaded = list(fresh.replay_orders())
    assert [o.client_order_id for o in loaded] == ["co-1", "co-2"]
    assert fresh.health()["n_parse_errors"] == 0
    assert fresh.health()["n_records_loaded"] == 2


# ── health() surface ──────────────────────────────────────────────────────────


def test_health_reports_zero_counters_on_fresh_ledger(tmp_path: Path):
    ledger = PaperLedger(tmp_path)
    h = ledger.health()
    assert h["n_records_loaded"] == 0
    assert h["n_parse_errors"] == 0
    assert h["orders_path"].endswith("orders.jsonl")
    assert h["fills_path"].endswith("fills.jsonl")
    assert h["settlements_path"].endswith("settlements.jsonl")


def test_health_counters_accumulate_across_streams(tmp_path: Path):
    """The two counters are global to the ledger instance, not per-file."""
    ledger = PaperLedger(tmp_path)
    ledger.append_order(_make_order())
    ledger.append_fill(_make_fill())
    ledger.append_settlement(_make_settlement())

    fresh = PaperLedger(tmp_path)
    list(fresh.replay_orders())
    list(fresh.replay_fills())
    list(fresh.replay_settlements())

    assert fresh.health()["n_records_loaded"] == 3
    assert fresh.health()["n_parse_errors"] == 0


def test_replay_missing_file_yields_nothing(tmp_path: Path):
    """A non-existent JSONL file is treated as empty, not an error."""
    ledger = PaperLedger(tmp_path)
    # No appends — files do not exist on disk yet.
    assert list(ledger.replay_orders()) == []
    assert list(ledger.replay_fills()) == []
    assert list(ledger.replay_settlements()) == []
    assert ledger.health()["n_parse_errors"] == 0
