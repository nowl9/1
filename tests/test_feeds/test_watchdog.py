"""Tests for feeds/watchdog.py -- the recorder silence/disk watchdog.

Added after the 2026-06-10 silent capture stop (all six --record-feeds
streams dead for 11 hours with no alarm).  Deterministic tests drive
``check_once()`` with an injected monotonic clock and a duck-typed fake
recorder; one async test runs the real loop against a real
FrameRecorder.  The file-log helpers are covered here too.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import btc_pm_arb.feeds.watchdog as watchdog_mod
from btc_pm_arb.feeds.recorder import (
    FrameRecorder,
    configure_recorder_file_log,
    file_log,
    reset_recorder_file_log,
)
from btc_pm_arb.feeds.watchdog import (
    SENTINEL_WATCHDOG_ALARM,
    RecorderWatchdog,
)
from btc_pm_arb.models import DataSource


class FakeRecorder:
    """Duck-type of the FrameRecorder surface the watchdog reads."""

    def __init__(self) -> None:
        self.last_writes: dict[str, float] = {}
        self.fatal: str | None = None

    def last_write_monotonic(self) -> dict[str, float]:
        return dict(self.last_writes)

    @property
    def fatal_reason(self) -> str | None:
        return self.fatal


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _make(
    tmp_path: Path,
    *,
    streams=("deribit",),
    restarters=None,
    max_restarts=3,
    silence=120.0,
    disk_soft_gb=0.0,
):
    rec = FakeRecorder()
    clock = Clock()
    beeps: list[int] = []
    wd = RecorderWatchdog(
        rec,  # type: ignore[arg-type] -- duck-typed on purpose
        tmp_path,
        streams=streams,
        silence_threshold_s=silence,
        check_interval_s=0.01,
        disk_soft_free_bytes=int(disk_soft_gb * 2**30),
        restarters=restarters,
        max_restarts_per_stream=max_restarts,
        monotonic=clock,
        beeper=lambda: beeps.append(1),
    )
    return rec, clock, wd, beeps


def _sentinel_lines(tmp_path: Path) -> list[str]:
    p = tmp_path / SENTINEL_WATCHDOG_ALARM
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="ascii").splitlines() if ln]


# ── Stream silence ───────────────────────────────────────────────────────────


def test_silent_stream_alarms_once_per_episode(tmp_path: Path) -> None:
    rec, clock, wd, beeps = _make(tmp_path)
    wd.check_once()  # t=0: baseline, no alarm
    assert _sentinel_lines(tmp_path) == []

    clock.now = 130.0  # silent-from-birth > 120s threshold
    wd.check_once()
    lines = _sentinel_lines(tmp_path)
    assert len(lines) == 1
    assert "kind=stream_silent" in lines[0]
    assert "stream=deribit" in lines[0]
    assert lines[0].startswith("ts=")
    assert beeps == [1]

    clock.now = 200.0  # still the same silence episode: no re-alarm
    wd.check_once()
    assert len(_sentinel_lines(tmp_path)) == 1
    assert beeps == [1]


def test_recovery_rearms_then_realarms(tmp_path: Path) -> None:
    rec, clock, wd, beeps = _make(tmp_path)
    wd.check_once()
    clock.now = 130.0
    wd.check_once()
    assert len(_sentinel_lines(tmp_path)) == 1

    rec.last_writes["deribit"] = 129.0  # stream wrote again -> recovered
    clock.now = 131.0
    wd.check_once()
    # New silence episode alarms again.
    clock.now = 260.0
    wd.check_once()
    lines = _sentinel_lines(tmp_path)
    assert len(lines) == 2
    assert all("kind=stream_silent" in ln for ln in lines)
    assert beeps == [1, 1]


def test_never_written_stream_alarms_from_watchdog_start(
    tmp_path: Path,
) -> None:
    """A stream that never writes a single frame (e.g. blocked RPC) must
    alarm after the threshold -- not never."""
    rec, clock, wd, beeps = _make(tmp_path, streams=("chainlink",))
    clock.now = 50.0
    wd.check_once()  # baseline established at 50
    clock.now = 100.0
    wd.check_once()  # age 50 < 120: quiet
    assert _sentinel_lines(tmp_path) == []
    clock.now = 171.0
    wd.check_once()  # age 121 > 120: alarm
    assert len(_sentinel_lines(tmp_path)) == 1


# ── Supervised restart ───────────────────────────────────────────────────────


def test_restart_hook_called_once_per_episode_with_cap(
    tmp_path: Path,
) -> None:
    calls: list[int] = []
    rec, clock, wd, _ = _make(
        tmp_path,
        restarters={"deribit": lambda: calls.append(1) or True},
        max_restarts=2,
    )
    wd.check_once()

    # Episode 1: alarm + restart.
    clock.now = 130.0
    wd.check_once()
    assert len(calls) == 1
    # Same episode persists: no second restart.
    clock.now = 200.0
    wd.check_once()
    assert len(calls) == 1

    # Recover, then episode 2: second (last budgeted) restart.
    rec.last_writes["deribit"] = 200.0
    clock.now = 201.0
    wd.check_once()
    clock.now = 330.0
    wd.check_once()
    assert len(calls) == 2

    # Recover, then episode 3: budget exhausted -> alarm but NO restart.
    rec.last_writes["deribit"] = 330.0
    clock.now = 331.0
    wd.check_once()
    clock.now = 460.0
    wd.check_once()
    assert len(calls) == 2
    assert len(_sentinel_lines(tmp_path)) == 3  # three alarms regardless


def test_missing_restart_hook_is_not_fatal(tmp_path: Path) -> None:
    rec, clock, wd, _ = _make(tmp_path, restarters={})
    wd.check_once()
    clock.now = 130.0
    wd.check_once()  # must not raise
    assert len(_sentinel_lines(tmp_path)) == 1


def test_raising_restart_hook_is_contained(tmp_path: Path) -> None:
    def _boom() -> bool:
        raise RuntimeError("restart exploded")

    rec, clock, wd, _ = _make(tmp_path, restarters={"deribit": _boom})
    wd.check_once()
    clock.now = 130.0
    wd.check_once()  # must not raise
    assert len(_sentinel_lines(tmp_path)) == 1


def test_raising_beeper_is_contained(tmp_path: Path) -> None:
    rec = FakeRecorder()
    clock = Clock()

    def _bad_beep() -> None:
        raise OSError("no sound device")

    wd = RecorderWatchdog(
        rec,  # type: ignore[arg-type]
        tmp_path,
        streams=("deribit",),
        silence_threshold_s=120.0,
        monotonic=clock,
        beeper=_bad_beep,
    )
    wd.check_once()
    clock.now = 130.0
    wd.check_once()  # must not raise
    assert len(_sentinel_lines(tmp_path)) == 1


# ── Disk soft alarm ──────────────────────────────────────────────────────────


def test_disk_soft_alarm_once_with_hysteresis(
    tmp_path: Path, monkeypatch,
) -> None:
    rec, clock, wd, beeps = _make(tmp_path, streams=(), disk_soft_gb=5.0)
    free_holder = {"free": 1 * 2**30}
    monkeypatch.setattr(
        watchdog_mod.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=0, used=0, free=free_holder["free"]),
    )

    wd.check_once()  # below floor -> alarm
    lines = _sentinel_lines(tmp_path)
    assert len(lines) == 1
    assert "kind=disk_low" in lines[0]

    wd.check_once()  # still below: same episode, no re-alarm
    assert len(_sentinel_lines(tmp_path)) == 1

    free_holder["free"] = 10 * 2**30  # recovered above floor + hysteresis
    wd.check_once()
    free_holder["free"] = 1 * 2**30  # drops again -> second alarm
    wd.check_once()
    assert len(_sentinel_lines(tmp_path)) == 2


# ── Recorder-fatal echo ──────────────────────────────────────────────────────


def test_recorder_fatal_echoes_one_alarm_only(tmp_path: Path) -> None:
    rec, clock, wd, _ = _make(tmp_path)
    rec.fatal = "io_error"
    wd.check_once()
    wd.check_once()
    lines = _sentinel_lines(tmp_path)
    assert len(lines) == 1
    assert "kind=recorder_fatal" in lines[0]
    assert "reason=io_error" in lines[0]


# ── Real-recorder integration (async loop) ───────────────────────────────────


async def test_watchdog_loop_alarms_on_real_recorder_silence(
    tmp_path: Path,
) -> None:
    """End-to-end: a real FrameRecorder writes once, goes silent, and the
    watchdog run() loop alarms within a few check intervals."""
    rec_dir = tmp_path / "recordings"
    rec = FrameRecorder(rec_dir)
    rec.record(DataSource.DERIBIT, "frame", datetime.now(timezone.utc))
    beeps: list[int] = []
    wd = RecorderWatchdog(
        rec,
        rec_dir,
        streams=("deribit",),
        silence_threshold_s=0.05,
        check_interval_s=0.02,
        disk_soft_free_bytes=0,  # disk check effectively off
        beeper=lambda: beeps.append(1),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(wd.run(stop))
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if (rec_dir / SENTINEL_WATCHDOG_ALARM).exists():
                break
        lines = _sentinel_lines(rec_dir)
        assert lines and "stream=deribit" in lines[0]
        assert beeps
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        rec.close()


def test_recorder_tracks_last_write(tmp_path: Path) -> None:
    rec = FrameRecorder(tmp_path)
    assert rec.last_write_monotonic() == {}
    rec.record(DataSource.DERIBIT, "frame", datetime.now(timezone.utc))
    stamps = rec.last_write_monotonic()
    assert set(stamps) == {"deribit"}
    rec.close()


# ── File log helpers ─────────────────────────────────────────────────────────


def test_file_log_writes_and_reset_closes(tmp_path: Path) -> None:
    log_path = tmp_path / "recorder.log"
    try:
        configure_recorder_file_log(log_path)
        file_log(logging.CRITICAL, "watchdog test line kind=unit")
        content = log_path.read_text(encoding="ascii")
        assert "watchdog test line kind=unit" in content
        assert "CRITICAL" in content
    finally:
        reset_recorder_file_log()


def test_file_log_noop_when_unconfigured(tmp_path: Path) -> None:
    reset_recorder_file_log()
    file_log(logging.CRITICAL, "goes nowhere")  # must not raise


def test_configure_is_idempotent_single_handler(tmp_path: Path) -> None:
    try:
        lg = configure_recorder_file_log(tmp_path / "a.log")
        lg = configure_recorder_file_log(tmp_path / "a.log")
        assert len(lg.handlers) == 1
        lg = configure_recorder_file_log(tmp_path / "b.log")
        assert len(lg.handlers) == 1
    finally:
        reset_recorder_file_log()
