"""Tests for tools/tail_funnel.py — rolling reject-rate observability.

Scope (Round 9c Commit 1):
* Window counts match a known synthetic record distribution.
* Arrow marker fires on a regime-shifted reason and not on others.
* Empty ledger / empty window returns the sentinel string.
* CLI argument parsing for --windows surfaces bad inputs as argparse errors.

The refresh loop itself (with ``time.sleep``) is not directly tested —
``_refresh_once`` is testable in isolation and the surrounding loop is
trivial plumbing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from btc_pm_arb.execution.paper_ledger import PaperLedger, PaperRejectionRecord
from btc_pm_arb.models import DataSource
from tools.tail_funnel import (
    _parse_windows,
    _refresh_once,
    render,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _rec(
    *,
    minutes_ago: float,
    reason_key: str,
    edge: float = 0.01,
) -> PaperRejectionRecord:
    """Build a rejection record at a specified offset before ``_NOW``."""
    return PaperRejectionRecord(
        timestamp=_NOW - timedelta(minutes=minutes_ago),
        contract_id=f"contract-{reason_key}",
        platform=DataSource.KALSHI,
        reason_key=reason_key,
        full_reason=f"{reason_key} 0.0050 < min 0.01",
        best_conservative_edge=edge,
        vol_regime="normal",
    )


# ── render() — counts ─────────────────────────────────────────────────────────


def test_render_counts_match_synthetic_distribution() -> None:
    """Known 2h fixture with mixed reasons → assert per-window counts."""
    records = [
        # Reason "alpha": 3 in last 30 min, 5 total in last 2h.
        _rec(minutes_ago=5,   reason_key="alpha"),
        _rec(minutes_ago=15,  reason_key="alpha"),
        _rec(minutes_ago=25,  reason_key="alpha"),
        _rec(minutes_ago=70,  reason_key="alpha"),
        _rec(minutes_ago=110, reason_key="alpha"),
        # Reason "beta": 2 in last 1h (20, 45 min), 4 in last 2h, 8 in last 6h.
        _rec(minutes_ago=20,  reason_key="beta"),
        _rec(minutes_ago=45,  reason_key="beta"),
        _rec(minutes_ago=80,  reason_key="beta"),
        _rec(minutes_ago=115, reason_key="beta"),
        _rec(minutes_ago=200, reason_key="beta"),
        _rec(minutes_ago=250, reason_key="beta"),
        _rec(minutes_ago=300, reason_key="beta"),
        _rec(minutes_ago=350, reason_key="beta"),
    ]

    out = render(
        records, windows_hours=[1.0, 6.0], now=_NOW, arrow_threshold=1.5,
    )

    # alpha: 1h=3 (5, 15, 25 min), 6h=5 (all five)
    # beta:  1h=2 (20, 45 min), 6h=8 (all eight)
    # Each line should contain the expected per-window count strings.
    lines = out.splitlines()
    alpha_line = next(ln for ln in lines if ln.startswith("alpha"))
    beta_line = next(ln for ln in lines if ln.startswith("beta"))
    assert "1h=3" in alpha_line
    assert "6h=5" in alpha_line
    assert "1h=2" in beta_line
    assert "6h=8" in beta_line


def test_render_sorted_by_first_window_descending() -> None:
    """First window's count is the sort key."""
    records = [
        _rec(minutes_ago=5,  reason_key="small"),                  # 1h=1
        _rec(minutes_ago=10, reason_key="big"),                    # 1h=3
        _rec(minutes_ago=20, reason_key="big"),
        _rec(minutes_ago=30, reason_key="big"),
        _rec(minutes_ago=15, reason_key="medium"),                 # 1h=2
        _rec(minutes_ago=25, reason_key="medium"),
    ]
    out = render(records, windows_hours=[1.0, 6.0], now=_NOW)
    lines = out.splitlines()
    reason_order = [ln.split()[0] for ln in lines]
    assert reason_order == ["big", "medium", "small"]


# ── render() — arrow ─────────────────────────────────────────────────────────


def test_render_arrow_fires_on_regime_shift() -> None:
    """Regime-shifted reason gets ←; steady-state reason does not.

    Setup: reason "shifted" has 7 rejections in the last 30 min and only
    those 7 in the 6h window → 1h_rate (7) > 1.5 × 7/6 (1.75) → arrow.

    Reason "steady" has 12 rejections spread evenly across 6h, so 1h=2,
    6h=12, extrapolated 1h=2, 2 > 1.5×2=3 is false → no arrow.
    """
    shifted = [_rec(minutes_ago=m, reason_key="shifted") for m in (3, 7, 12, 18, 23, 27, 29)]
    # Evenly spread across 6h: every 30 min.
    steady = [_rec(minutes_ago=m, reason_key="steady") for m in (15, 45, 75, 105, 135, 165, 195, 225, 255, 285, 315, 345)]
    out = render(shifted + steady, windows_hours=[1.0, 6.0], now=_NOW)

    lines = {ln.split()[0]: ln for ln in out.splitlines()}
    assert "shifted" in lines and "steady" in lines
    assert "←" in lines["shifted"]
    assert "←" not in lines["steady"]


def test_render_arrow_suppressed_below_min_6h() -> None:
    """Small-sample noise: a reason with only 3 rejections (all in last
    30 min) would arithmetic-arrow (3 > 1.5 × 0.5) but the 6h-count
    minimum suppresses it.  This is the small-sample guard documented in
    the module-level ``_MIN_ARROW_6H`` comment."""
    tiny = [_rec(minutes_ago=m, reason_key="tiny") for m in (5, 15, 25)]
    out = render(tiny, windows_hours=[1.0, 6.0], now=_NOW, arrow_threshold=1.5)
    line = next(ln for ln in out.splitlines() if ln.startswith("tiny"))
    assert "1h=3" in line
    assert "6h=3" in line
    assert "←" not in line


def test_render_arrow_disabled_when_1h_and_6h_not_both_present() -> None:
    """Arrow logic is only meaningful for the (1h, 6h) pair — when the
    user passes other windows, the column is rendered but no arrows
    appear regardless of the count distribution."""
    records = [_rec(minutes_ago=m, reason_key="x") for m in (3, 7, 12, 18, 23, 27, 29)]
    out = render(records, windows_hours=[1.0, 24.0], now=_NOW)
    assert "←" not in out


# ── render() — empty ─────────────────────────────────────────────────────────


def test_render_empty_records_returns_sentinel() -> None:
    """Empty input → sentinel line, not empty string."""
    out = render([], windows_hours=[1.0, 6.0], now=_NOW)
    assert out == "no rejections logged yet"


def test_render_records_all_outside_window_returns_sentinel() -> None:
    """All records are older than max(windows) → no per-reason buckets
    populated → sentinel.  Note: _refresh_once filters by cutoff before
    calling render, so this scenario is mostly hypothetical for the
    integration path, but render() is a public-ish helper and the
    fallback matters for direct callers."""
    records = [_rec(minutes_ago=24 * 60, reason_key="old")]
    out = render(records, windows_hours=[1.0, 6.0], now=_NOW)
    assert out == "no rejections logged yet"


# ── _refresh_once — integration with PaperLedger ──────────────────────────────


def test_refresh_once_reads_from_ledger(tmp_path: Path) -> None:
    """Light integration: write rejections to a ledger, call _refresh_once
    with an injected ``now``, assert the resulting block contains the
    expected reason and counts."""
    ledger = PaperLedger(tmp_path)
    for m in (5, 10, 15):
        ledger.append_rejection(_rec(minutes_ago=m, reason_key="alpha"))
    ledger.append_rejection(_rec(minutes_ago=200, reason_key="beta"))

    out = _refresh_once(
        ledger, windows_hours=[1.0, 6.0], arrow_threshold=1.5, now=_NOW,
    )
    assert "tail_funnel @" in out
    assert "alpha" in out
    assert "beta" in out
    assert "1h=3" in out  # alpha
    # beta is at 200 min ago → outside 1h, inside 6h
    beta_line = next(ln for ln in out.splitlines() if ln.startswith("beta"))
    assert "1h=0" in beta_line
    assert "6h=1" in beta_line


def test_refresh_once_empty_ledger(tmp_path: Path) -> None:
    """No rejections.jsonl yet → sentinel string in the body."""
    ledger = PaperLedger(tmp_path)
    out = _refresh_once(
        ledger, windows_hours=[1.0, 6.0], arrow_threshold=1.5, now=_NOW,
    )
    assert "0 rejection(s) in last 6h" in out
    assert "no rejections logged yet" in out


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_parse_windows_valid() -> None:
    assert _parse_windows("1,6") == [1.0, 6.0]
    assert _parse_windows("3") == [3.0]
    assert _parse_windows("0.5, 2") == [0.5, 2.0]


def test_parse_windows_rejects_invalid() -> None:
    for bad in ("", "0,1", "-1,2", "a"):
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_windows(bad)
