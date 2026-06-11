"""Tests for _install_stop_signals (C4, 2026-06-10 clean shutdown).

One Ctrl-C / Ctrl-Break / SIGTERM must set stop_event so every task
winds down and run()'s finally reaches recorder.close(), banking every
recording gzip with a valid trailer.  SIGBREAK (Windows) is registered
so a supervisor can deliver the same stop path programmatically via
GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT) -- which is exactly what the
Phase-3 Ctrl-C drill does.
"""

from __future__ import annotations

import asyncio
import signal

import btc_pm_arb.main as main_mod
from btc_pm_arb.main import _install_stop_signals


async def test_signal_handlers_set_stop_event_via_signal_module(
    monkeypatch,
) -> None:
    """Windows path: add_signal_handler raises NotImplementedError and
    the classic signal.signal handlers are installed instead.  Invoking
    any of them sets stop_event."""
    loop = asyncio.get_running_loop()

    def _unsupported(*_a, **_k):
        raise NotImplementedError

    monkeypatch.setattr(
        loop, "add_signal_handler", _unsupported, raising=False,
    )
    captured: dict[int, object] = {}
    monkeypatch.setattr(
        main_mod.signal,
        "signal",
        lambda sig, handler: captured.__setitem__(int(sig), handler),
    )

    stop = asyncio.Event()
    _install_stop_signals(stop)

    expected = {int(signal.SIGINT), int(signal.SIGTERM)}
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        expected.add(int(sigbreak))
    assert set(captured) == expected

    assert not stop.is_set()
    handler = captured[int(signal.SIGINT)]
    handler(int(signal.SIGINT), None)  # type: ignore[operator]
    assert stop.is_set()


async def test_signal_handlers_via_loop_when_supported(monkeypatch) -> None:
    """POSIX-style path: add_signal_handler accepts the callback."""
    loop = asyncio.get_running_loop()
    registered: dict[int, object] = {}
    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda sig, cb: registered.__setitem__(int(sig), cb),
        raising=False,
    )

    stop = asyncio.Event()
    _install_stop_signals(stop)

    assert int(signal.SIGINT) in registered
    assert int(signal.SIGTERM) in registered
    registered[int(signal.SIGINT)]()  # type: ignore[operator]
    assert stop.is_set()


async def test_sigbreak_handler_sets_stop_event_on_windows(
    monkeypatch,
) -> None:
    """The Ctrl-Break path (used by the shutdown drill and by operators
    closing via Ctrl-Break) is wired to the same stop_event."""
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:  # non-Windows: nothing to assert
        return
    loop = asyncio.get_running_loop()

    def _unsupported(*_a, **_k):
        raise NotImplementedError

    monkeypatch.setattr(
        loop, "add_signal_handler", _unsupported, raising=False,
    )
    captured: dict[int, object] = {}
    monkeypatch.setattr(
        main_mod.signal,
        "signal",
        lambda sig, handler: captured.__setitem__(int(sig), handler),
    )
    stop = asyncio.Event()
    _install_stop_signals(stop)
    captured[int(sigbreak)](int(sigbreak), None)  # type: ignore[operator]
    assert stop.is_set()
