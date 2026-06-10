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
* Failure policy (revised 2026-06-10, graceful-stop-loud): on the first
  OSError the recorder closes EVERY handle with valid gzip trailers
  (banking buffered frames), logs CRITICAL, appends the RECORDER_FAILED
  sentinel, and fires on_fatal exactly once; subsequent calls no-op and
  do not raise.  Close failures are logged, never silently swallowed.
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


def test_io_error_fails_loud_and_banks_all_streams(tmp_path: Path) -> None:
    """First OSError -> ALL open handles closed with valid trailers,
    RECORDER_FAILED sentinel written, on_fatal fired once, subsequent
    record() calls no-op without raising (2026-06-10 policy)."""
    fatal_calls: list[str] = []
    rec = FrameRecorder(tmp_path, on_fatal=fatal_calls.append)
    rec.record(DataSource.DERIBIT, "d-ok", _T0)
    rec.record(DataSource.KALSHI, "k-ok", _T0, endpoint="/markets")
    assert rec._disabled is False

    # Simulate ENOSPC on the next write (the 2026-06-10 trigger).
    with patch.object(
        FrameRecorder, "_handle_for",
        side_effect=OSError(28, "No space left on device"),
    ):
        # Must not raise into the feed callback.
        rec.record(DataSource.POLYMARKET, "dropped", _T0)

    assert rec._disabled is True
    assert fatal_calls == ["io_error"]

    # Subsequent call is a no-op and must NOT re-fire on_fatal.
    rec.record(DataSource.DERIBIT, "still-stopped", _T0 + timedelta(hours=2))
    assert fatal_calls == ["io_error"]

    # Both already-open streams banked their buffered frames WITH valid
    # gzip trailers (gzip.open raises EOFError on a trailerless file).
    p_d = tmp_path / "deribit" / "2026-05-25" / "frames-14.jsonl.gz"
    p_k = tmp_path / "kalshi" / "2026-05-25" / "frames-14.jsonl.gz"
    with gzip.open(p_d, "rt", encoding="utf-8") as f:
        assert [json.loads(ln)["frame"] for ln in f] == ["d-ok"]
    with gzip.open(p_k, "rt", encoding="utf-8") as f:
        assert [json.loads(ln)["frame"] for ln in f] == ["k-ok"]

    # Sentinel exists and names the failure.
    sentinel = (tmp_path / "RECORDER_FAILED").read_text(encoding="ascii")
    assert "reason=io_error" in sentinel
    assert "error_type=OSError" in sentinel
    assert "close_failures=none" in sentinel


def test_fatal_close_failure_is_recorded_not_swallowed(
    tmp_path: Path, capsys,
) -> None:
    """A handle whose close() raises during the fatal stop is named in
    the sentinel and logged CRITICAL -- never silently swallowed."""

    class _BrokenClose:
        def write(self, data: bytes) -> None:  # pragma: no cover
            raise OSError(28, "No space left on device")

        def close(self) -> None:
            raise OSError(28, "No space left on device (at close)")

    rec = FrameRecorder(tmp_path)
    rec.record(DataSource.DERIBIT, "d-ok", _T0)
    # Inject a broken handle for a second stream, then trip the failure.
    path = tmp_path / "kalshi" / "2026-05-25" / "frames-14.jsonl.gz"
    rec._handles["kalshi"] = (path, _BrokenClose())  # type: ignore[assignment]
    with patch.object(
        FrameRecorder, "_handle_for",
        side_effect=OSError(28, "No space left on device"),
    ):
        rec.record(DataSource.POLYMARKET, "dropped", _T0)

    sentinel = (tmp_path / "RECORDER_FAILED").read_text(encoding="ascii")
    assert "close_failures=kalshi" in sentinel
    out = capsys.readouterr().out
    assert "frame_recorder.fatal_close_failed" in out
    # The healthy stream still banked its frames with a trailer.
    p_d = tmp_path / "deribit" / "2026-05-25" / "frames-14.jsonl.gz"
    with gzip.open(p_d, "rt", encoding="utf-8") as f:
        assert [json.loads(ln)["frame"] for ln in f] == ["d-ok"]


def test_on_fatal_callback_exception_is_contained(tmp_path: Path) -> None:
    """record() must never raise into feed callbacks -- even when the
    on_fatal callback itself blows up."""

    def _bad_callback(reason: str) -> None:
        raise RuntimeError("callback exploded")

    rec = FrameRecorder(tmp_path, on_fatal=_bad_callback)
    with patch.object(
        FrameRecorder, "_handle_for", side_effect=OSError("simulated"),
    ):
        rec.record(DataSource.DERIBIT, "x", _T0)  # must not raise
    assert rec._disabled is True


def test_close_after_fatal_does_not_raise(tmp_path: Path) -> None:
    """close() must be safe after the recorder has fatal-stopped."""
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
