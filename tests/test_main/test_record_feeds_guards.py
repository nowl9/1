"""Tests for the --record-feeds capture-path trust guards (2026-06-10).

Scope:
* Preflight: ``_record_feeds_preflight`` measures free space via
  shutil.disk_usage (never file-size sums) and refuses to start below
  the configurable floor (``recorder_min_free_gb``, default 20 GiB).
* ``run()`` returns exit code 2 when the preflight refuses, without
  constructing the agent or touching the network.

Background: on 2026-06-10 a disk-full (ENOSPC) silently stopped all six
capture streams 2.7 h into an overnight --record-feeds run.  The
preflight makes "disk too full to record" a loud start-time refusal
instead of a silent mid-run death.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import btc_pm_arb.main as main_mod
from btc_pm_arb.main import _record_feeds_preflight, run


def _fake_usage(free_bytes: int):
    return lambda _path: SimpleNamespace(
        total=1000 * 2**30, used=0, free=free_bytes,
    )


def test_preflight_refuses_below_floor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_mod.settings, "recorder_min_free_gb", 20.0)
    monkeypatch.setattr(main_mod.shutil, "disk_usage", _fake_usage(2 * 2**30))
    assert _record_feeds_preflight(tmp_path) is False


def test_preflight_passes_above_floor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_mod.settings, "recorder_min_free_gb", 20.0)
    monkeypatch.setattr(main_mod.shutil, "disk_usage", _fake_usage(98 * 2**30))
    assert _record_feeds_preflight(tmp_path) is True


def test_preflight_floor_is_configurable(tmp_path: Path, monkeypatch) -> None:
    """Floor comes from settings, not a hard-coded constant."""
    monkeypatch.setattr(main_mod.settings, "recorder_min_free_gb", 1.0)
    monkeypatch.setattr(main_mod.shutil, "disk_usage", _fake_usage(2 * 2**30))
    assert _record_feeds_preflight(tmp_path) is True
    monkeypatch.setattr(main_mod.settings, "recorder_min_free_gb", 4.0)
    assert _record_feeds_preflight(tmp_path) is False


def test_preflight_walks_to_existing_ancestor(
    tmp_path: Path, monkeypatch,
) -> None:
    """record_dir may not exist yet -- preflight probes the nearest
    existing ancestor rather than crashing."""
    probed: list[Path] = []

    def _spy(path):
        probed.append(Path(path))
        return SimpleNamespace(total=0, used=0, free=98 * 2**30)

    monkeypatch.setattr(main_mod.settings, "recorder_min_free_gb", 20.0)
    monkeypatch.setattr(main_mod.shutil, "disk_usage", _spy)
    missing = tmp_path / "not" / "yet" / "created"
    assert _record_feeds_preflight(missing) is True
    assert probed and probed[0].exists()


async def test_run_returns_2_when_preflight_refuses(
    tmp_path: Path, monkeypatch,
) -> None:
    """A refused preflight exits the run with code 2 before the agent
    (and any network feed) is constructed."""
    monkeypatch.setattr(
        main_mod, "_record_feeds_preflight", lambda _p: False,
    )
    code = await run(dry_run=True, record_dir=tmp_path, mode="live")
    assert code == 2
