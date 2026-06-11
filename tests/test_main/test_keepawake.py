"""Tests for keepawake.py (C5) -- SetThreadExecutionState scope.

The scope must arm ES_CONTINUOUS|ES_SYSTEM_REQUIRED on entry, release
with ES_CONTINUOUS on ANY exit path (normal, exception), stay inert
when disabled, and contain setter failures (keep-awake is a
convenience, never a reason to kill a capture run).
"""

from __future__ import annotations

import pytest

from btc_pm_arb.keepawake import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    keep_system_awake,
)


def test_armed_and_released_in_order() -> None:
    calls: list[int] = []

    def _setter(flags: int) -> int:
        calls.append(flags)
        return 1

    with keep_system_awake(True, _setter=_setter) as armed:
        assert armed is True
        assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]


def test_disabled_scope_never_touches_setter() -> None:
    calls: list[int] = []
    with keep_system_awake(False, _setter=lambda f: calls.append(f) or 1) as armed:
        assert armed is False
    assert calls == []


def test_released_even_when_body_raises() -> None:
    calls: list[int] = []

    def _setter(flags: int) -> int:
        calls.append(flags)
        return 1

    with pytest.raises(RuntimeError):
        with keep_system_awake(True, _setter=_setter):
            raise RuntimeError("capture blew up")
    assert calls[-1] == ES_CONTINUOUS


def test_setter_failure_yields_unarmed_and_skips_release() -> None:
    calls: list[int] = []

    def _setter(flags: int) -> int:
        calls.append(flags)
        return 0  # API-level failure

    with keep_system_awake(True, _setter=_setter) as armed:
        assert armed is False
    # No release call for a state that was never armed.
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]


def test_setter_exception_is_contained() -> None:
    def _setter(flags: int) -> int:
        raise OSError("no kernel32 here")

    with keep_system_awake(True, _setter=_setter) as armed:
        assert armed is False  # must not raise
