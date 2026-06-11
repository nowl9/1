"""Tests for the _dashboard_task stop_event fix (2026-06-10 C3).

Defect: the original _dashboard_task awaited ``uvicorn.Server.serve()``
unconditionally.  uvicorn's own signal handling is disabled in-process,
so serve() never returned, the TaskGroup never exited, ``--duration``
runs never completed, and NO run path ever reached recorder.close() --
the cause of the final-hour gzip truncation on every long capture run.

These tests stub uvicorn.Server (no real socket) and assert the task
now honors stop_event: it asks uvicorn to exit via ``should_exit`` and
returns promptly; a crashing serve() is logged and returns instead of
wedging the group.
"""

from __future__ import annotations

import asyncio

import btc_pm_arb.main as main_mod
from btc_pm_arb.main import Agent, _dashboard_task, _duration_task


class FakeServer:
    """Stub of the uvicorn.Server surface _dashboard_task touches."""

    last: "FakeServer | None" = None

    def __init__(self, config) -> None:
        self.config = config
        self.should_exit = False
        self.serve_entered = False
        FakeServer.last = self

    def install_signal_handlers(self) -> None:  # replaced by the task
        raise AssertionError("should have been stubbed out")

    async def serve(self) -> None:
        self.serve_entered = True
        while not self.should_exit:
            await asyncio.sleep(0.005)


class CrashingServer(FakeServer):
    async def serve(self) -> None:
        self.serve_entered = True
        raise RuntimeError("bind failed")


async def test_dashboard_task_honors_stop_event(monkeypatch) -> None:
    monkeypatch.setattr(main_mod.uvicorn, "Server", FakeServer)
    agent = Agent(dry_run=True)
    stop = asyncio.Event()
    task = asyncio.create_task(_dashboard_task(agent, stop))
    await asyncio.sleep(0.05)
    assert not task.done()
    assert FakeServer.last is not None and FakeServer.last.serve_entered

    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert FakeServer.last.should_exit is True


async def test_dashboard_task_returns_when_serve_crashes(
    monkeypatch,
) -> None:
    """A crashed dashboard logs and returns -- it must not wedge the
    TaskGroup nor take down the run."""
    monkeypatch.setattr(main_mod.uvicorn, "Server", CrashingServer)
    agent = Agent(dry_run=True)
    stop = asyncio.Event()
    await asyncio.wait_for(_dashboard_task(agent, stop), timeout=2.0)


async def test_duration_then_dashboard_completes(monkeypatch) -> None:
    """--duration semantics at unit level: the duration task sets
    stop_event and the dashboard task (the 2026-06-10 wedge) follows it
    out, so a bounded run can reach recorder.close()."""
    monkeypatch.setattr(main_mod.uvicorn, "Server", FakeServer)
    agent = Agent(dry_run=True)
    stop = asyncio.Event()
    dash = asyncio.create_task(_dashboard_task(agent, stop))
    dur = asyncio.create_task(_duration_task(stop, 0.1))
    await asyncio.wait_for(asyncio.gather(dash, dur), timeout=3.0)
    assert stop.is_set()
