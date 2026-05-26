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

Failure policy (Round 9c Commit 2 / Q4)
---------------------------------------
On the first ``OSError`` / ``IOError`` (disk full, permission denied,
etc.) the recorder is **disabled for the rest of the process
lifetime** — it logs a WARNING and all subsequent ``record()`` calls
no-op.  Recording is non-essential to live operation; crashing the
agent because we can't write a recording would be the wrong tradeoff.

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
import threading
from datetime import datetime
from pathlib import Path
from typing import IO

import structlog

from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# Default per-source per-day soft cap before emitting a WARNING.  5 GiB
# matches the Round 9c plan.  No auto-stop on exceeding — see module
# docstring.
_DEFAULT_MAX_DAILY_BYTES: int = 5 * 2**30


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
    ) -> None:
        self._base_dir = Path(base_dir)
        self._max_daily_bytes = max_daily_bytes
        # source.value -> (current_path, open gzip handle).  One handle
        # per source; rotated on hour boundary.
        self._handles: dict[str, tuple[Path, IO[bytes]]] = {}
        # (source.value, "YYYY-MM-DD") -> bytes-written cumulative for the day.
        self._daily_bytes: dict[tuple[str, str], int] = {}
        # (source.value, "YYYY-MM-DD") for which the over-budget WARNING
        # has already been emitted — prevents log spam.
        self._warned: set[tuple[str, str]] = set()
        self._disabled = False
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def record(
        self,
        source: DataSource,
        frame: bytes | str | dict,
        ts: datetime,
        *,
        endpoint: str | None = None,
    ) -> None:
        """Append a single frame to the day/hour file for ``source``.

        Encoding of the ``frame`` argument:
        * ``str``: written verbatim.
        * ``bytes``: utf-8-decoded.  On ``UnicodeDecodeError``, base64-
          encoded with a ``<b64>`` prefix so a replay reader can detect
          and decode.  Our three feeds emit utf-8 JSON, so the b64 path
          is purely defensive.
        * ``dict``: JSON-encoded via ``json.dumps(default=str)``.

        On the first ``OSError`` / ``IOError`` the recorder disables
        itself; subsequent calls no-op.  See module docstring.
        """
        if self._disabled:
            return
        try:
            payload = {
                "ts": ts.isoformat(),
                "source": source.value,
                "endpoint": endpoint,
                "frame": _to_text(frame),
            }
            data = (json.dumps(payload) + "\n").encode("utf-8")

            with self._lock:
                handle = self._handle_for(source, ts)
                handle.write(data)
                self._track_daily(source, ts, len(data))
        except (OSError, IOError) as exc:
            self._disable(reason="io_error", exc=exc)

    def close(self) -> None:
        """Close all open file handles.  Safe to call after ``_disable``."""
        with self._lock:
            for _path, h in self._handles.values():
                try:
                    h.close()
                except Exception:
                    pass
            self._handles.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _path_for(self, source: DataSource, ts: datetime) -> Path:
        day = ts.strftime("%Y-%m-%d")
        hour = ts.strftime("%H")
        return self._base_dir / source.value / day / f"frames-{hour}.jsonl.gz"

    def _handle_for(self, source: DataSource, ts: datetime) -> IO[bytes]:
        """Return a writable gzip handle for the (source, hour) of ``ts``.

        Rotates on hour boundary: if the cached handle's path differs
        from the target path, close the cached handle and open a new one.
        """
        target = self._path_for(source, ts)
        current = self._handles.get(source.value)
        if current is not None and current[0] == target:
            return current[1]
        # Rotation: close the old handle before opening the new.
        if current is not None:
            try:
                current[1].close()
            except Exception:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        new_handle = gzip.open(target, mode="ab")
        self._handles[source.value] = (target, new_handle)
        return new_handle

    def _track_daily(self, source: DataSource, ts: datetime, n_bytes: int) -> None:
        day = ts.strftime("%Y-%m-%d")
        key = (source.value, day)
        self._daily_bytes[key] = self._daily_bytes.get(key, 0) + n_bytes
        if (
            self._daily_bytes[key] > self._max_daily_bytes
            and key not in self._warned
        ):
            self._warned.add(key)
            logger.warning(
                "frame_recorder.daily_budget_exceeded",
                source=source.value,
                day=day,
                bytes_written=self._daily_bytes[key],
                max_daily_bytes=self._max_daily_bytes,
            )

    def _disable(self, *, reason: str, exc: Exception) -> None:
        """Permanently disable the recorder for the rest of the process.

        Called from inside the write lock.  Closes cached handles
        directly (rather than via :meth:`close`, which re-acquires the
        non-reentrant lock) so any buffered gzip data for previously
        successful frames is flushed to disk before the references are
        dropped.  Without the flush, the last few frames written before
        the failure would be lost in the unclosed gzip block.
        """
        self._disabled = True
        logger.warning(
            "frame_recorder.disabled",
            reason=reason,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        for _path, h in self._handles.values():
            try:
                h.close()
            except Exception:
                pass
        self._handles.clear()


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
