"""Recorder watchdog -- per-stream silence + disk free-space monitor.

Added after the 2026-06-10 silent capture stop: all six --record-feeds
streams died at 08:01:25 UTC and NOTHING noticed for 11 hours.  The
watchdog's job is to make the next capture-path failure loud within
seconds, leave on-disk evidence, and (where a restart hook exists)
attempt a supervised restart of the dead component.

Checks (every ``check_interval_s``):

* **Stream silence** -- each expected stream's last-successful-write age
  (``FrameRecorder.last_write_monotonic()``).  A stream silent longer
  than ``silence_threshold_s`` (config ``recorder_watchdog_silence_s``,
  default 120 s) raises one alarm per silence episode; recovery re-arms
  it.  If the orchestrator registered a restarter for the stream, the
  watchdog calls it (capped at ``max_restarts_per_stream`` per run).
* **Disk free space** -- ``shutil.disk_usage`` on the recording volume
  (never file-size sums).  Free space below ``disk_soft_free_bytes``
  (config ``recorder_disk_soft_free_gb``, default 5 GiB) raises one
  alarm per crossing, while writes still work -- the operator gets a
  window to free space BEFORE the hard ENOSPC kills the capture.

Alarm channels -- deliberately layered so the alarm does not depend on
the failing resource:

1. console: structlog CRITICAL (stdout);
2. file:    the recorder file log (``outputs/recorder.log``, see
            :func:`btc_pm_arb.feeds.recorder.configure_recorder_file_log`);
3. disk:    ``WATCHDOG_ALARM`` sentinel appended under ``base_dir``
            (reason + UTC timestamp per line);
4. audible: ``ctypes`` MessageBeep on Windows (no disk, no network).

Every channel is best-effort and exception-contained: the watchdog must
never take down the run it is guarding.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import structlog

from btc_pm_arb.feeds.recorder import FrameRecorder, file_log

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# Sentinel filename appended under ``base_dir`` on every watchdog alarm.
SENTINEL_WATCHDOG_ALARM: str = "WATCHDOG_ALARM"


def _default_beep() -> None:
    """Audible, disk-independent tertiary alarm channel (Windows)."""
    if sys.platform == "win32":
        import ctypes

        # MB_ICONHAND-style system sound; 0xFFFFFFFF = simple beep
        # fallback when no sound scheme is configured.
        ctypes.windll.user32.MessageBeep(0xFFFFFFFF)


def _existing_ancestor(path: Path) -> Path:
    """Nearest existing ancestor of ``path`` (for shutil.disk_usage)."""
    probe = path.resolve()
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return probe


class RecorderWatchdog:
    """Monitor a :class:`FrameRecorder`'s streams and recording volume.

    Pure-asyncio; one instance per --record-feeds run.  All collaborator
    seams (clock, beeper, restarters) are injectable for deterministic
    tests.
    """

    def __init__(
        self,
        recorder: FrameRecorder,
        base_dir: Path | str,
        *,
        streams: tuple[str, ...] | list[str],
        silence_threshold_s: float = 120.0,
        check_interval_s: float = 10.0,
        disk_soft_free_bytes: int = 5 * 2**30,
        restarters: dict[str, Callable[[], bool]] | None = None,
        max_restarts_per_stream: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
        beeper: Callable[[], None] = _default_beep,
    ) -> None:
        self._recorder = recorder
        self._base_dir = Path(base_dir)
        self._streams = tuple(streams)
        self._silence_threshold_s = silence_threshold_s
        self._check_interval_s = check_interval_s
        self._disk_soft_free_bytes = disk_soft_free_bytes
        self._restarters = dict(restarters or {})
        self._max_restarts_per_stream = max_restarts_per_stream
        self._monotonic = monotonic
        self._beeper = beeper
        # Baseline for streams that have never written: watchdog start
        # time, so a stream silent-from-birth alarms after the threshold
        # instead of never.
        self._started: float | None = None
        # Streams currently in an alarmed silence episode (re-armed on
        # recovery so the next episode alarms again).
        self._alarmed_streams: set[str] = set()
        self._restart_counts: dict[str, int] = {}
        self._disk_alarmed = False
        self._recorder_fatal_alarmed = False

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        self._started = self._monotonic()
        logger.info(
            "recorder_watchdog.starting",
            streams=list(self._streams),
            silence_threshold_s=self._silence_threshold_s,
            check_interval_s=self._check_interval_s,
            disk_soft_free_gb=round(self._disk_soft_free_bytes / 2**30, 2),
        )
        file_log(
            logging.INFO,
            "recorder_watchdog.starting streams=%s silence_threshold_s=%s"
            % (",".join(self._streams), self._silence_threshold_s),
        )
        while not stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 -- never kill the run
                logger.error(
                    "recorder_watchdog.check_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._check_interval_s,
                )
            except asyncio.TimeoutError:
                pass
        logger.info("recorder_watchdog.stopped")

    # ── One scan (sync; injectable-clock-driven; unit-testable) ───────────────

    def check_once(self) -> None:
        """Run one watchdog scan: recorder-fatal, stream ages, disk."""
        if self._started is None:
            self._started = self._monotonic()
        if self._recorder.fatal_reason is not None:
            # The fatal path already alarmed loudly and is stopping the
            # run; emit one watchdog echo (not per-stream spam).
            if not self._recorder_fatal_alarmed:
                self._recorder_fatal_alarmed = True
                self._alarm(
                    kind="recorder_fatal",
                    detail="reason=%s" % self._recorder.fatal_reason,
                )
            return
        self._check_streams()
        self._check_disk()

    def _check_streams(self) -> None:
        now = self._monotonic()
        last_writes = self._recorder.last_write_monotonic()
        for stream in self._streams:
            assert self._started is not None
            last = last_writes.get(stream, self._started)
            age = now - last
            if age > self._silence_threshold_s:
                if stream not in self._alarmed_streams:
                    self._alarmed_streams.add(stream)
                    self._alarm(
                        kind="stream_silent",
                        stream=stream,
                        detail="silent_s=%.1f threshold_s=%.1f"
                        % (age, self._silence_threshold_s),
                    )
                    self._attempt_restart(stream)
            elif stream in self._alarmed_streams:
                self._alarmed_streams.discard(stream)
                logger.info(
                    "recorder_watchdog.stream_recovered",
                    stream=stream,
                )
                file_log(
                    logging.INFO,
                    "recorder_watchdog.stream_recovered stream=%s" % stream,
                )

    def _check_disk(self) -> None:
        try:
            free = shutil.disk_usage(_existing_ancestor(self._base_dir)).free
        except OSError as exc:
            logger.error(
                "recorder_watchdog.disk_probe_error", error=str(exc),
            )
            return
        if free < self._disk_soft_free_bytes:
            if not self._disk_alarmed:
                self._disk_alarmed = True
                self._alarm(
                    kind="disk_low",
                    detail="free_gb=%.2f soft_floor_gb=%.2f"
                    % (free / 2**30, self._disk_soft_free_bytes / 2**30),
                )
        elif self._disk_alarmed and free >= self._disk_soft_free_bytes * 1.05:
            # 5% hysteresis so the alarm does not flap at the floor.
            self._disk_alarmed = False
            logger.info(
                "recorder_watchdog.disk_recovered",
                free_gb=round(free / 2**30, 2),
            )
            file_log(
                logging.INFO,
                "recorder_watchdog.disk_recovered free_gb=%.2f"
                % (free / 2**30),
            )

    # ── Alarm + restart ────────────────────────────────────────────────────────

    def _alarm(
        self, *, kind: str, detail: str, stream: str | None = None,
    ) -> None:
        """Fire all alarm channels.  Each is independently best-effort."""
        # 1. console (structlog CRITICAL)
        logger.critical(
            "recorder_watchdog.alarm",
            kind=kind,
            stream=stream,
            detail=detail,
        )
        # 2. recorder file log
        file_log(
            logging.CRITICAL,
            "recorder_watchdog.alarm kind=%s stream=%s %s"
            % (kind, stream or "-", detail),
        )
        # 3. sentinel file under the recording base dir
        line = "ts=%s kind=%s stream=%s %s\n" % (
            datetime.now(timezone.utc).isoformat(),
            kind,
            stream or "-",
            detail,
        )
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            sentinel = self._base_dir / SENTINEL_WATCHDOG_ALARM
            with open(sentinel, "a", encoding="ascii", errors="replace") as f:
                f.write(line)
        except Exception as exc:  # disk may be the failing resource
            logger.critical(
                "recorder_watchdog.sentinel_write_failed", error=str(exc),
            )
        # 4. audible, disk-independent
        try:
            self._beeper()
        except Exception:
            pass

    def _attempt_restart(self, stream: str) -> None:
        restarter = self._restarters.get(stream)
        if restarter is None:
            logger.warning(
                "recorder_watchdog.no_restart_hook", stream=stream,
            )
            return
        count = self._restart_counts.get(stream, 0)
        if count >= self._max_restarts_per_stream:
            logger.critical(
                "recorder_watchdog.restart_budget_exhausted",
                stream=stream,
                attempts=count,
            )
            file_log(
                logging.CRITICAL,
                "recorder_watchdog.restart_budget_exhausted stream=%s "
                "attempts=%d" % (stream, count),
            )
            return
        self._restart_counts[stream] = count + 1
        try:
            initiated = bool(restarter())
        except Exception as exc:  # noqa: BLE001 -- restart must not kill us
            initiated = False
            logger.error(
                "recorder_watchdog.restart_hook_error",
                stream=stream,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        logger.warning(
            "recorder_watchdog.restart_attempted",
            stream=stream,
            attempt=self._restart_counts[stream],
            initiated=initiated,
        )
        file_log(
            logging.WARNING,
            "recorder_watchdog.restart_attempted stream=%s attempt=%d "
            "initiated=%s" % (stream, self._restart_counts[stream], initiated),
        )
