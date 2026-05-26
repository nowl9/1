"""Tests for the Round 9c addition: PaperRejectionRecord + ledger I/O.

Scope (Round 9c Commit 1):
* Schema round-trip: PaperRejectionRecord writes and reads back equal to the original.
* schema_version defaults to 1 (consistent with the other three record types).
* Skip-and-warn-with-counter applies to rejections.jsonl too: a malformed
  mid-file line increments n_parse_errors and surrounding records still load.
* health() surfaces the new rejections_path.
* Records from the four streams accumulate independently on the same ledger.

Tests live in a separate file from test_paper_ledger.py so the original
Round 8 schema regression suite stays focused on the original three
record types.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_pm_arb.execution.paper_ledger import (
    PaperLedger,
    PaperRejectionRecord,
)
from btc_pm_arb.models import DataSource


# ── Fixtures / helpers ────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _make_rejection(
    *,
    timestamp: datetime | None = None,
    contract_id: str = "KXBTC-26MAY01-100000",
    platform: DataSource = DataSource.KALSHI,
    reason_key: str = "conservative_edge",
    full_reason: str = "conservative_edge 0.0050 < min 0.01",
    best_conservative_edge: float = 0.005,
    vol_regime: str = "normal",
) -> PaperRejectionRecord:
    return PaperRejectionRecord(
        timestamp=timestamp or _NOW,
        contract_id=contract_id,
        platform=platform,
        reason_key=reason_key,
        full_reason=full_reason,
        best_conservative_edge=best_conservative_edge,
        vol_regime=vol_regime,
    )


# ── Schema round-trip ────────────────────────────────────────────────────────


def test_paper_rejection_record_round_trip(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path)
    original = _make_rejection()
    ledger.append_rejection(original)

    loaded = list(ledger.replay_rejections())
    assert len(loaded) == 1
    assert loaded[0] == original


def test_schema_version_defaults_to_one(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path)
    rec = _make_rejection()
    assert rec.schema_version == 1
    ledger.append_rejection(rec)
    [loaded] = list(ledger.replay_rejections())
    assert loaded.schema_version == 1


def test_multiple_rejections_replay_in_append_order(tmp_path: Path) -> None:
    """Append-only ordering is preserved on replay — required for the
    time-window logic in tail_funnel to be order-independent (it sorts by
    timestamp internally) but also helpful for forensic log-walking."""
    ledger = PaperLedger(tmp_path)
    recs = [
        _make_rejection(
            timestamp=_NOW + timedelta(minutes=i),
            reason_key=("a" if i % 2 == 0 else "b"),
        )
        for i in range(5)
    ]
    for r in recs:
        ledger.append_rejection(r)

    loaded = list(ledger.replay_rejections())
    assert loaded == recs


# ── Skip-and-warn ────────────────────────────────────────────────────────────


def test_malformed_mid_file_line_skipped(tmp_path: Path) -> None:
    """Same skip-and-warn-with-counter policy as the other three streams.

    Round 8 Commit 1 established the policy; this test asserts the new
    fourth stream participates correctly (the policy lives in the
    shared ``_replay`` helper, but a regression in the wiring of the
    new file path could miss this).
    """
    ledger = PaperLedger(tmp_path)
    ledger.append_rejection(_make_rejection(reason_key="first"))
    # Manually inject a malformed line between two valid records.
    with open(ledger.health()["rejections_path"], "a", encoding="utf-8") as f:
        f.write("not-json-at-all\n")
    ledger.append_rejection(_make_rejection(reason_key="third"))

    # Use a fresh PaperLedger so the counter starts at zero — easier to
    # assert against than the appender's accumulated counter.
    fresh = PaperLedger(tmp_path)
    loaded = list(fresh.replay_rejections())
    keys = [r.reason_key for r in loaded]
    assert keys == ["first", "third"]
    assert fresh.health()["n_records_loaded"] == 2
    assert fresh.health()["n_parse_errors"] == 1


# ── health() surfaces the new path ────────────────────────────────────────────


def test_health_surfaces_rejections_path(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path)
    h = ledger.health()
    assert "rejections_path" in h
    assert h["rejections_path"].endswith("rejections.jsonl")
    # The four streams' paths are all under the ledger's base_dir.
    assert str(tmp_path) in h["rejections_path"]


def test_health_counters_include_rejections(tmp_path: Path) -> None:
    """Reading rejections increments n_records_loaded on the same counter
    used by the other three streams.  Documented behaviour:
    ``n_records_loaded`` is cumulative across all replay calls on the
    instance (see PaperLedger.health docstring), so a rejection replay
    must contribute to it."""
    ledger = PaperLedger(tmp_path)
    ledger.append_rejection(_make_rejection())
    ledger.append_rejection(_make_rejection(reason_key="other"))

    _ = list(ledger.replay_rejections())
    assert ledger.health()["n_records_loaded"] == 2
    assert ledger.health()["n_parse_errors"] == 0


# ── Missing-file ─────────────────────────────────────────────────────────────


def test_replay_missing_rejections_file_yields_nothing(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path)
    # Nothing appended → file does not exist.
    assert list(ledger.replay_rejections()) == []
    assert ledger.health()["n_records_loaded"] == 0
