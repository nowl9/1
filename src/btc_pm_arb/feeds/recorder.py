"""Append-only frame recorder for replay-mode validation of feed data.

Round 9c Commit 2 addition.  Captures raw frames from the three live
feeds (Deribit WS, Kalshi REST, Polymarket REST) into gzipped JSONL
files so a future replay-mode harness can rerun the agent against
historical conditions.  **Disabled by default** — feed handlers
construct without a recorder unless ``main.py`` is invoked with
``--record-feeds``.

Design
------
* One file per ``(source, day, hour-of-day)`` under
  ``{base_dir}/{source}/{YYYY-MM-DD}/frames-HH.jsonl.gz``.  Hourly
  rotation keeps per-file size bounded.
* One open gzip handle per source at a time, re-opened on the hour
  rollover.  Avoids open/close-per-frame overhead at Deribit's
  hundreds-of-frames-per-second cadence.
* Wire shape per JSONL line::

    {"ts": "<iso8601>", "source": "deribit", "endpoint": null,
     "frame": "<utf-8 string of the raw frame>"}

  For Deribit ``frame`` is the WebSocket message string; for Kalshi /
  Polymarket it is the HTTP response body decoded as utf-8.
  ``endpoint`` distinguishes the per-feed REST paths (Kalshi's
  ``/markets`` vs ``/markets/{ticker}/orderbook``; Polymarket's gamma
  ``/markets`` vs CLOB ``/book``).  None for Deribit (no per-frame
  endpoint).

Failure policy (revised 2026-06-10: graceful-stop-LOUD)
-------------------------------------------------------
History: the original Round 9c policy ("on the first OSError, log one
WARNING and silently no-op forever") converted a transient disk-full
(ENOSPC) on 2026-06-10 into an 11-hour silent capture outage -- all
six streams stopped at once, the close failures were swallowed, and
``close()`` at shutdown was a no-op because ``_handles`` had been
cleared.  See outputs/diag_repro_disable.py for the confirmed repro.

Current policy: on the first unrecoverable ``OSError`` / ``IOError``
the recorder *fails loud*:

* every open gzip handle is closed (banking all buffered frames and
  writing valid trailers) -- per-handle close failures are logged
  CRITICAL, never swallowed;
* one CRITICAL ``frame_recorder.fatal`` log line is emitted;
* a ``RECORDER_FAILED`` sentinel (reason + timestamp + close results)
  is appended under ``base_dir`` so the failure leaves on-disk
  evidence even if the console scrolls away;
* the optional ``on_fatal`` callback fires exactly once so the
  orchestrator can stop the run and exit nonzero.

There is no silent continuation and no retry loop: a capture run that
cannot write is worthless, so it stops, loudly.  ``record()`` itself
still never raises into feed callbacks -- subsequent calls no-op while
the orchestrator winds the run down.

Disk-usage guard
----------------
Per-(source, day) bytes-written is tracked in memory.  Crossing
``max_daily_bytes`` (default 5 GiB) emits one WARNING per (source,
day) bucket and continues writing — no auto-stop.  Operators see the
warning in agent.log and intervene.

Crash atomicity
---------------
gzip writes are flushed lazily by the gzip block buffer and are NOT
fsynced.  A ``kill -9`` mid-write may truncate the last few frames in
the trailing gzip block.  Acceptable — this is research data, not a
financial record.  The paper-trading ledger (orders / fills /
settlements / rejections) is the integrity-critical persistence
layer and uses ``os.fsync`` per append; see ``paper_ledger.py``.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable

import structlog

from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# Default per-source per-day soft cap before emitting a WARNING.  5 GiB
# matches the Round 9c plan.  No auto-stop on exceeding — see module
# docstring.
_DEFAULT_MAX_DAILY_BYTES: int = 5 * 2**30

# Sentinel filename appended under ``base_dir`` when the recorder fails
# fatally.  Operators (and the next diagnostic session) look here first.
SENTINEL_RECORDER_FAILED: str = "RECORDER_FAILED"


# ── Persistent file log (2026-06-10: the fatal WARNING was stdout-only and
#    scrolled away unrecorded; the next failure must leave evidence) ──────────

_FILE_LOGGER_NAME = "btc_pm_arb.recorder_file"


def configure_recorder_file_log(path: Path | str) -> logging.Logger:
    """Attach a persistent file handler for recorder/watchdog events.

    Replaces any previously configured handler (idempotent across
    repeated calls).  The logger does NOT propagate to the root logger
    -- it is a dedicated evidence channel, independent of the stdout
    structlog pipeline.  ASCII-only by construction.
    """
    lg = logging.getLogger(_FILE_LOGGER_NAME)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    reset_recorder_file_log()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(
        p, encoding="ascii", errors="replace", delay=True,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    lg.addHandler(handler)
    return lg


def reset_recorder_file_log() -> None:
    """Close and detach all handlers from the recorder file logger."""
    lg = logging.getLogger(_FILE_LOGGER_NAME)
    for handler in list(lg.handlers):
        try:
            handler.close()
        except Exception:
            pass
        lg.removeHandler(handler)


def file_log(level: int, message: str) -> None:
    """Best-effort write to the recorder file log (no-op if unconfigured).

    Never raises: the file log is an alarm channel and must not become
    a new failure source (e.g. when the disk itself is the problem).
    """
    lg = logging.getLogger(_FILE_LOGGER_NAME)
    if not lg.handlers:
        return
    try:
        lg.log(level, message)
    except Exception:
        pass


class FrameRecorder:
    """Append-only gzipped-JSONL recorder for raw feed frames.

    Threadsafe-ish: a ``threading.Lock`` serialises writes so a future
    multi-threaded use case can't interleave bytes in the gzip stream.
    asyncio is single-threaded, so the lock is uncontended in normal
    operation; the cost is negligible.
    """

    def __init__(
        self,
        base_dir: Path | str,
        *,
        max_daily_bytes: int = _DEFAULT_MAX_DAILY_BYTES,
        on_fatal: Callable[[str], None] | None = None,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._max_daily_bytes = max_daily_bytes
        # Fired exactly once on fatal I/O failure (after handles are
        # closed and the sentinel is written) so the orchestrator can
        # stop the run and exit nonzero.  Exceptions it raises are
        # contained -- record() must never raise into feed callbacks.
        self._on_fatal = on_fatal
        self._fatal_reason: str | None = None
        # source.value -> (current_path, open gzip handle).  One handle
        # per source; rotated on hour boundary.
        self._handles: dict[str, tuple[Path, IO[bytes]]] = {}
        # (source.value, "YYYY-MM-DD") -> bytes-written cumulative for the day.
        self._daily_bytes: dict[tuple[str, str], int] = {}
        # (source.value, "YYYY-MM-DD") for which the over-budget WARNING
        # has already been emitted — prevents log spam.
        self._warned: set[tuple[str, str]] = set()
        # source.value -> time.monotonic() of the last successful write.
        # Read by the RecorderWatchdog to detect silently-dead streams.
        self._last_write: dict[str, float] = {}
        self._disabled = False
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def record(
        self,
        source: DataSource | str,
        frame: bytes | str | dict,
        ts: datetime,
        *,
        endpoint: str | None = None,
    ) -> None:
        """Append a single frame to the day/hour file for ``source``.

        ``source`` may be a :class:`DataSource` enum member (the three
        live trading feeds: deribit / kalshi / polymarket) or a bare
        ``str`` source tag for the capture-only auxiliary streams added
        under ``--record-feeds`` (e.g. ``"spot"``, ``"chainlink"``,
        ``"pm5min"``).  Bare-string tags are recording-only labels and
        deliberately NOT members of :class:`DataSource` -- they never
        flow into pricing, signals, gates, or execution.  For a
        ``DataSource`` the on-disk ``source`` field is ``source.value``,
        byte-identical to before this overload existed.

        Encoding of the ``frame`` argument:
        * ``str``: written verbatim.
        * ``bytes``: utf-8-decoded.  On ``UnicodeDecodeError``, base64-
          encoded with a ``<b64>`` prefix so a replay reader can detect
          and decode.  Our three feeds emit utf-8 JSON, so the b64 path
          is purely defensive.
        * ``dict``: JSON-encoded via ``json.dumps(default=str)``.

        On the first ``OSError`` / ``IOError`` the recorder fails LOUD
        (closes every handle with trailers, logs CRITICAL, writes the
        ``RECORDER_FAILED`` sentinel, fires ``on_fatal``); subsequent
        calls no-op while the run winds down.  See module docstring.
        """
        if self._disabled:
            return
        source_value = source.value if isinstance(source, DataSource) else source
        try:
            payload = {
                "ts": ts.isoformat(),
                "source": source_value,
                "endpoint": endpoint,
                "frame": _to_text(frame),
            }
            data = (json.dumps(payload) + "\n").encode("utf-8")

            with self._lock:
                handle = self._handle_for(source_value, ts)
                handle.write(data)
                self._track_daily(source_value, ts, len(data))
                self._last_write[source_value] = time.monotonic()
        except (OSError, IOError) as exc:
            self._fail(reason="io_error", exc=exc)

    def last_write_monotonic(self) -> dict[str, float]:
        """Snapshot of per-stream ``time.monotonic()`` last-write stamps."""
        with self._lock:
            return dict(self._last_write)

    @property
    def fatal_reason(self) -> str | None:
        """Non-None once the recorder has fatal-stopped (see :meth:`_fail`)."""
        return self._fatal_reason

    def close(self) -> None:
        """Close all open file handles, banking buffered frames + trailers.

        Safe to call after :meth:`_fail` (no-op: handles already
        cleared).  Per-handle close failures are logged CRITICAL --
        never silently swallowed -- but do not raise: ``close()`` runs
        in shutdown ``finally`` blocks where raising would mask the
        original error.
        """
        with self._lock:
            for source_value, (path, h) in self._handles.items():
                try:
                    h.close()
                except Exception as exc:
                    logger.critical(
                        "frame_recorder.close_failed",
                        source=source_value,
                        path=str(path),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
            self._handles.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _path_for(self, source_value: str, ts: datetime) -> Path:
        day = ts.strftime("%Y-%m-%d")
        hour = ts.strftime("%H")
        return self._base_dir / source_value / day / f"frames-{hour}.jsonl.gz"

    def _handle_for(self, source_value: str, ts: datetime) -> IO[bytes]:
        """Return a writable gzip handle for the (source, hour) of ``ts``.

        Rotates on hour boundary: if the cached handle's path differs
        from the target path, close the cached handle and open a new one.
        """
        target = self._path_for(source_value, ts)
        current = self._handles.get(source_value)
        if current is not None and current[0] == target:
            return current[1]
        # Rotation: close the old handle before opening the new.
        if current is not None:
            try:
                current[1].close()
            except Exception as exc:
                # A failed rotation close loses the old hour's buffered
                # tail -- that must never be silent.
                logger.critical(
                    "frame_recorder.rotation_close_failed",
                    source=source_value,
                    path=str(current[0]),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        new_handle = gzip.open(target, mode="ab")
        self._handles[source_value] = (target, new_handle)
        return new_handle

    def _track_daily(self, source_value: str, ts: datetime, n_bytes: int) -> None:
        day = ts.strftime("%Y-%m-%d")
        key = (source_value, day)
        self._daily_bytes[key] = self._daily_bytes.get(key, 0) + n_bytes
        if (
            self._daily_bytes[key] > self._max_daily_bytes
            and key not in self._warned
        ):
            self._warned.add(key)
            logger.warning(
                "frame_recorder.daily_budget_exceeded",
                source=source_value,
                day=day,
                bytes_written=self._daily_bytes[key],
                max_daily_bytes=self._max_daily_bytes,
            )

    def _fail(self, *, reason: str, exc: Exception) -> None:
        """Fatal-stop the recorder LOUDLY (2026-06-10 policy revision).

        Called from :meth:`record`'s except handler, i.e. *outside* the
        write lock (the ``with`` block has already released it on the
        way out).  Re-acquires the lock, closes every cached handle so
        all buffered gzip data is banked with valid trailers, logs
        CRITICAL, appends a ``RECORDER_FAILED`` sentinel under
        ``base_dir``, and fires ``on_fatal`` exactly once so the
        orchestrator stops the run and exits nonzero.  Never raises.
        """
        with self._lock:
            if self._fatal_reason is not None:
                return  # already failed; never double-fire
            self._fatal_reason = reason
            self._disabled = True
            logger.critical(
                "frame_recorder.fatal",
                reason=reason,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            file_log(
                logging.CRITICAL,
                "frame_recorder.fatal reason=%s error_type=%s error=%s"
                % (reason, type(exc).__name__, str(exc).replace("\n", " ")),
            )
            close_failures: list[str] = []
            for source_value, (path, h) in self._handles.items():
                try:
                    h.close()
                except Exception as close_exc:
                    close_failures.append(source_value)
                    logger.critical(
                        "frame_recorder.fatal_close_failed",
                        source=source_value,
                        path=str(path),
                        error_type=type(close_exc).__name__,
                        error=str(close_exc),
                    )
                    file_log(
                        logging.CRITICAL,
                        "frame_recorder.fatal_close_failed source=%s "
                        "path=%s error=%s"
                        % (source_value, path, str(close_exc).replace("\n", " ")),
                    )
            self._handles.clear()
        self._write_failed_sentinel(
            reason=reason, exc=exc, close_failures=close_failures,
        )
        if self._on_fatal is not None:
            try:
                self._on_fatal(reason)
            except Exception as cb_exc:
                logger.critical(
                    "frame_recorder.on_fatal_callback_error",
                    error_type=type(cb_exc).__name__,
                    error=str(cb_exc),
                )

    def _write_failed_sentinel(
        self, *, reason: str, exc: Exception, close_failures: list[str],
    ) -> None:
        """Append one ASCII line to ``base_dir/RECORDER_FAILED``.

        Best-effort: the disk may be the very thing that failed.  A
        sentinel-write failure is itself logged CRITICAL (the console /
        file log are the remaining channels).
        """
        line = (
            "ts=%s reason=%s error_type=%s error=%s close_failures=%s\n"
            % (
                datetime.now(timezone.utc).isoformat(),
                reason,
                type(exc).__name__,
                str(exc).replace("\n", " "),
                ",".join(close_failures) if close_failures else "none",
            )
        )
        sentinel = self._base_dir / SENTINEL_RECORDER_FAILED
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            with open(sentinel, "a", encoding="ascii", errors="replace") as f:
                f.write(line)
        except Exception as sentinel_exc:
            logger.critical(
                "frame_recorder.sentinel_write_failed",
                path=str(sentinel),
                error_type=type(sentinel_exc).__name__,
                error=str(sentinel_exc),
            )


# ── Encoding helper ───────────────────────────────────────────────────────────


def _to_text(frame: bytes | str | dict) -> str:
    """Coerce a frame payload to a utf-8 string.

    See :meth:`FrameRecorder.record` docstring for the encoding rules.
    """
    if isinstance(frame, str):
        return frame
    if isinstance(frame, bytes):
        try:
            return frame.decode("utf-8")
        except UnicodeDecodeError:
            return "<b64>" + base64.b64encode(frame).decode("ascii")
    if isinstance(frame, dict):
        return json.dumps(frame, default=str)
    # Unexpected type — fall back to repr.  Defensive only; the three
    # feed call sites always pass one of the three types above.
    return repr(frame)
