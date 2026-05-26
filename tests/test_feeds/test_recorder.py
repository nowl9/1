"""Tests for feeds/recorder.py — gzipped-JSONL frame recorder.

Scope (Round 9c Commit 2):
* Roundtrip: record N frames, gunzip and parse back, assert content
  equality and timestamp ordering.
* Endpoint field round-trips verbatim (REST feeds need it to distinguish
  discovery vs orderbook responses on replay).
* Bytes input is utf-8 decoded into the wire ``frame`` field.
* Hourly rotation: frames across an hour boundary land in distinct
  ``frames-HH.jsonl.gz`` files under the same day directory.
* Daily rotation: frames across a day boundary land in distinct day
  directories.
* No files until the first record() — construction is side-effect-free.
* Failure policy (Q4): on the first OSError, recorder logs + disables
  itself; subsequent calls no-op and do not raise.
* _to_text encoding helper: str passthrough, bytes utf-8, bytes non-utf-8
  falls back to base64, dict JSON-encoded.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from btc_pm_arb.feeds.recorder import FrameRecorder, _to_text
from btc_pm_arb.models import DataSource


_T0 = datetime(2026, 5, 25, 14, 30, 0, tzinfo=timezone.utc)


# ── Roundtrip ────────────────────────────────────────────────────────────────


def test_roundtrip_record_and_read(tmp_path: Path) -> None:
    """Record 3 frames, gunzip the file, assert content + ordering."""
    rec = FrameRecorder(tmp_path)
    frames = [
        ("deribit-frame-1", _T0),
        ("deribit-frame-2", _T0 + timedelta(seconds=1)),
        ("deribit-frame-3", _T0 + timedelta(seconds=2)),
    ]
    for content, ts in frames:
        rec.record(DataSource.DERIBIT, content, ts)
    rec.close()

    expected = tmp_path / "deribit" / "2026-05-25" / "frames-14.jsonl.gz"
    assert expected.exists()
    with gzip.open(expected, "rt", encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f]
    assert len(lines) == 3
    assert [ln["frame"] for ln in lines] == [c for c, _ in frames]
    assert all(ln["source"] == "deribit" for ln in lines)
    # Timestamps strictly increasing.
    tss = [datetime.fromisoformat(ln["ts"]) for ln in lines]
    assert tss == sorted(tss)


def test_endpoint_field_preserved(tmp_path: Path) -> None:
    """REST feeds pass endpoint; replay tooling needs it preserved verbatim."""
    rec = FrameRecorder(tmp_path)
    rec.record(
        DataSource.KALSHI, b'{"markets": []}', _T0,
        endpoint="/markets?series_ticker=KXBTC&status=open&limit=200",
    )
    rec.close()

    path = tmp_path / "kalshi" / "2026-05-25" / "frames-14.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        [line] = [json.loads(ln) for ln in f]
    assert line["endpoint"] == (
        "/markets?series_ticker=KXBTC&status=open&limit=200"
    )
    assert line["frame"] == '{"markets": []}'


def test_endpoint_none_for_websocket_frames(tmp_path: Path) -> None:
    """Deribit frames pass no endpoint -> field is JSON null on disk."""
    rec = FrameRecorder(tmp_path)
    rec.record(DataSource.DERIBIT, "ws-frame", _T0)
    rec.close()
    path = tmp_path / "deribit" / "2026-05-25" / "frames-14.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        [line] = [json.loads(ln) for ln in f]
    assert line["endpoint"] is None


def test_record_bytes_utf8_decoded(tmp_path: Path) -> None:
    """Bytes input is utf-8 decoded into the frame field."""
    rec = FrameRecorder(tmp_path)
    rec.record(DataSource.POLYMARKET, b'{"hello": "world"}', _T0, endpoint="/book")
    rec.close()
    path = tmp_path / "polymarket" / "2026-05-25" / "frames-14.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        [line] = [json.loads(ln) for ln in f]
    assert line["frame"] == '{"hello": "world"}'


# ── Rotation ─────────────────────────────────────────────────────────────────


def test_hourly_rotation(tmp_path: Path) -> None:
    """Frames spanning an hour boundary land in distinct hour files."""
    rec = FrameRecorder(tmp_path)
    rec.record(DataSource.DERIBIT, "f-before-rollover", _T0.replace(minute=59))
    rec.record(
        DataSource.DERIBIT, "f-after-rollover", _T0.replace(hour=15, minute=1),
    )
    rec.close()

    p14 = tmp_path / "deribit" / "2026-05-25" / "frames-14.jsonl.gz"
    p15 = tmp_path / "deribit" / "2026-05-25" / "frames-15.jsonl.gz"
    assert p14.exists() and p15.exists()

    with gzip.open(p14, "rt", encoding="utf-8") as f:
        lines14 = [json.loads(ln) for ln in f]
    with gzip.open(p15, "rt", encoding="utf-8") as f:
        lines15 = [json.loads(ln) for ln in f]
    assert [ln["frame"] for ln in lines14] == ["f-before-rollover"]
    assert [ln["frame"] for ln in lines15] == ["f-after-rollover"]


def test_daily_rotation(tmp_path: Path) -> None:
    """Frames spanning a day boundary land in distinct day directories."""
    rec = FrameRecorder(tmp_path)
    rec.record(DataSource.DERIBIT, "f-day1", _T0)
    rec.record(DataSource.DERIBIT, "f-day2", _T0 + timedelta(days=1))
    rec.close()

    assert (tmp_path / "deribit" / "2026-05-25").exists()
    assert (tmp_path / "deribit" / "2026-05-26").exists()


def test_per_source_directories_isolated(tmp_path: Path) -> None:
    """Different sources land in different top-level directories."""
    rec = FrameRecorder(tmp_path)
    rec.record(DataSource.DERIBIT, "d", _T0)
    rec.record(DataSource.KALSHI, "k", _T0, endpoint="/markets")
    rec.record(DataSource.POLYMARKET, "p", _T0, endpoint="/book")
    rec.close()

    assert (tmp_path / "deribit").exists()
    assert (tmp_path / "kalshi").exists()
    assert (tmp_path / "polymarket").exists()


# ── Construction is side-effect-free ─────────────────────────────────────────


def test_no_files_created_until_first_record(tmp_path: Path) -> None:
    """Constructing a FrameRecorder writes nothing.  Feed handlers that
    take recorder=None never call record() at all; this test asserts the
    recorder itself is also idle on construction."""
    _ = FrameRecorder(tmp_path)
    assert list(tmp_path.iterdir()) == []


# ── Failure policy ───────────────────────────────────────────────────────────


def test_io_error_disables_recorder(tmp_path: Path) -> None:
    """On first OSError, recorder disables itself + no-ops subsequent calls."""
    rec = FrameRecorder(tmp_path)
    # First call succeeds.
    rec.record(DataSource.DERIBIT, "ok", _T0)
    assert rec._disabled is False

    # Force the next write to raise OSError by patching _handle_for.
    # Simulates a disk-full or permission error mid-run.
    with patch.object(
        FrameRecorder, "_handle_for", side_effect=OSError("simulated"),
    ):
        # Must not raise.
        rec.record(
            DataSource.DERIBIT, "should-be-dropped",
            _T0 + timedelta(hours=1),
        )

    assert rec._disabled is True

    # Subsequent call (without the patch) is also a no-op.
    rec.record(
        DataSource.DERIBIT, "still-disabled", _T0 + timedelta(hours=2),
    )

    # Read what landed on disk — the first "ok" frame, nothing else.
    p = tmp_path / "deribit" / "2026-05-25" / "frames-14.jsonl.gz"
    with gzip.open(p, "rt", encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f]
    assert [ln["frame"] for ln in lines] == ["ok"]


def test_close_after_disable_does_not_raise(tmp_path: Path) -> None:
    """close() must be safe after the recorder has self-disabled."""
    rec = FrameRecorder(tmp_path)
    with patch.object(
        FrameRecorder, "_handle_for", side_effect=OSError("simulated"),
    ):
        rec.record(DataSource.DERIBIT, "x", _T0)
    assert rec._disabled is True
    # No exception.
    rec.close()


# ── Daily-bytes guard ────────────────────────────────────────────────────────


def test_daily_budget_warning_fires_once(tmp_path: Path, caplog) -> None:
    """Crossing max_daily_bytes emits exactly one WARNING per (source, day)
    bucket — repeat crossings do not re-warn within the same bucket."""
    # Tiny budget so any record overflows it.
    rec = FrameRecorder(tmp_path, max_daily_bytes=10)
    rec.record(DataSource.DERIBIT, "a" * 50, _T0)
    rec.record(DataSource.DERIBIT, "b" * 50, _T0 + timedelta(seconds=1))
    rec.close()
    # Internal state: warned set contains exactly the (deribit, day) key.
    assert ("deribit", "2026-05-25") in rec._warned
    assert len(rec._warned) == 1


# ── _to_text helper ──────────────────────────────────────────────────────────


def test_to_text_str_passthrough() -> None:
    assert _to_text("hello") == "hello"


def test_to_text_bytes_utf8() -> None:
    assert _to_text(b"hello") == "hello"


def test_to_text_bytes_non_utf8_falls_back_to_b64() -> None:
    out = _to_text(b"\xff\xfe\xfd")
    assert out.startswith("<b64>")


def test_to_text_dict_json_encoded() -> None:
    out = _to_text({"k": "v"})
    assert json.loads(out) == {"k": "v"}
