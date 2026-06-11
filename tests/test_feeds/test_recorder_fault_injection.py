"""Fault-injection test for the 2026-06-10 silent-stop signature (C6).

Promoted from the Phase-1 diagnostic harness
(outputs/diag_repro_disable.py), which reproduced the field failure:
six concurrent capture streams, one injected ENOSPC (OSError errno 28)
on one stream's gzip handle.  Under the OLD policy that one error
silently killed all six streams forever, swallowed the close failures,
and left recorder.close() a no-op.

This test pins the NEW graceful-stop-loud behavior end to end:
* all six streams stop together, but every closable handle banks its
  buffered frames with a VALID gzip trailer;
* the failing stream's close failure is named in the RECORDER_FAILED
  sentinel and is not silently swallowed;
* the fatal alarm fires: on_fatal exactly once + sentinel on disk, and
  the RecorderWatchdog echoes kind=recorder_fatal on its next scan;
* subsequent record() calls no-op without raising while the run winds
  down; the event loop stays alive; a later close() is a safe no-op.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from btc_pm_arb.feeds.recorder import (
    SENTINEL_RECORDER_FAILED,
    FrameRecorder,
)
from btc_pm_arb.feeds.watchdog import (
    SENTINEL_WATCHDOG_ALARM,
    RecorderWatchdog,
)

STREAMS = ("kalshi", "chainlink", "pm5min", "polymarket", "spot", "deribit")
INJECTED = "polymarket"  # the stream that hit ENOSPC in the field


class EnospcHandle:
    """Wraps a live gzip handle: write AND close raise ENOSPC."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def write(self, data: bytes) -> None:
        raise OSError(28, "No space left on device (injected)")

    def close(self) -> None:
        raise OSError(28, "No space left on device (injected, at close)")


async def _stream_task(
    rec: FrameRecorder, name: str, stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        rec.record(name, '{"x": "%s"}' % ("y" * 200), datetime.now(timezone.utc))
        await asyncio.sleep(0.01)


def _hour_file(base: Path, stream: str) -> Path:
    now = datetime.now(timezone.utc)
    return (
        base
        / stream
        / now.strftime("%Y-%m-%d")
        / ("frames-%s.jsonl.gz" % now.strftime("%H"))
    )


async def test_one_enospc_stops_all_streams_loud_with_trailers(
    tmp_path: Path,
) -> None:
    fatal_calls: list[str] = []
    rec = FrameRecorder(tmp_path, on_fatal=fatal_calls.append)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(_stream_task(rec, s, stop)) for s in STREAMS
    ]
    try:
        # Warm-up: all six handles open and writing.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(rec._handles) == len(STREAMS):
                break
        assert len(rec._handles) == len(STREAMS)

        # INJECTION: the next polymarket write hits ENOSPC, exactly as
        # in the field at 2026-06-10T08:01:25Z.
        path, real = rec._handles[INJECTED]
        rec._handles[INJECTED] = (path, EnospcHandle(real))

        # Wait for the fatal stop to fire.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if fatal_calls:
                break
        assert fatal_calls == ["io_error"]
        assert rec.fatal_reason == "io_error"
        assert rec._disabled is True
        assert rec._handles == {}

        # Streams keep "recording" against the stopped recorder: no
        # raise, no growth, no on_fatal re-fire (loop stays alive --
        # we are running in it).
        sizes_1 = {
            s: _hour_file(tmp_path, s).stat().st_size for s in STREAMS
        }
        await asyncio.sleep(0.1)
        sizes_2 = {
            s: _hour_file(tmp_path, s).stat().st_size for s in STREAMS
        }
        assert sizes_1 == sizes_2
        assert fatal_calls == ["io_error"]
    finally:
        stop.set()
        await asyncio.gather(*tasks)

    # Five healthy streams banked their frames with VALID trailers
    # (gzip read to EOF raises EOFError on a trailerless file).
    for s in STREAMS:
        if s == INJECTED:
            continue
        with gzip.open(_hour_file(tmp_path, s), "rt", encoding="utf-8") as f:
            lines = [json.loads(ln) for ln in f]
        assert lines, "stream %s banked no frames" % s
        assert all(ln["source"] == s for ln in lines)

    # The failing stream's close failure is on the record -- loud, not
    # swallowed: sentinel names reason and the failed stream.
    sentinel = (tmp_path / SENTINEL_RECORDER_FAILED).read_text(
        encoding="ascii"
    )
    assert "reason=io_error" in sentinel
    assert "close_failures=%s" % INJECTED in sentinel

    # The watchdog's next scan echoes the fatal as a single alarm.
    wd = RecorderWatchdog(
        rec,
        tmp_path,
        streams=STREAMS,
        silence_threshold_s=120.0,
        beeper=lambda: None,
    )
    wd.check_once()
    wd.check_once()
    alarm_lines = [
        ln
        for ln in (tmp_path / SENTINEL_WATCHDOG_ALARM)
        .read_text(encoding="ascii")
        .splitlines()
        if ln
    ]
    assert len(alarm_lines) == 1
    assert "kind=recorder_fatal" in alarm_lines[0]

    # Late shutdown close (the 19:21 Ctrl-C analogue): safe no-op that
    # touches no file.
    before = {
        s: _hour_file(tmp_path, s).stat().st_mtime_ns for s in STREAMS
    }
    rec.close()
    after = {
        s: _hour_file(tmp_path, s).stat().st_mtime_ns for s in STREAMS
    }
    assert before == after
