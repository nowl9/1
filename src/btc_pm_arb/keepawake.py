"""Windows keep-awake scope for --record-feeds capture runs (C5).

Holds ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``
for the life of a capture run so the machine cannot sleep mid-capture,
and releases it (``ES_CONTINUOUS`` alone) on ANY exit path.

Context: the 2026-06-10 silent stop was NOT a sleep (Windows event logs
exonerated the OS -- the cause was ENOSPC), but system sleep is the
next-most-likely silent killer for overnight captures on a workstation,
so --record-feeds now pins the system awake for its duration.

The execution state is per-THREAD, so the scope must be entered on a
thread that lives for the whole run -- ``main()`` wraps ``asyncio.run``
with it on the main thread.  Display sleep is deliberately still
allowed (no ``ES_DISPLAY_REQUIRED``): the capture needs the machine,
not the monitor.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Callable, Iterator

import structlog

logger: structlog.BoundLogger = structlog.get_logger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _default_setter(flags: int) -> int:
    """Call kernel32.SetThreadExecutionState; 0 return means failure.

    No-op (returns 0) off Windows -- the scope simply stays unarmed.
    """
    if sys.platform != "win32":
        return 0
    import ctypes

    return int(ctypes.windll.kernel32.SetThreadExecutionState(flags))


@contextmanager
def keep_system_awake(
    enabled: bool,
    *,
    _setter: Callable[[int], int] = _default_setter,
) -> Iterator[bool]:
    """Hold ES_SYSTEM_REQUIRED while the body runs; release on any exit.

    Yields True when the keep-awake state was actually armed.  All
    failures are logged and contained -- keep-awake is a convenience,
    never a reason to refuse or kill a capture run.
    """
    armed = False
    if enabled:
        try:
            armed = _setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0
        except Exception as exc:  # noqa: BLE001 -- never block the run
            logger.warning(
                "keepawake.arm_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        if armed:
            logger.info("keepawake.armed")
        else:
            logger.warning("keepawake.not_armed")
    try:
        yield armed
    finally:
        if armed:
            try:
                _setter(ES_CONTINUOUS)
                logger.info("keepawake.released")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "keepawake.release_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
