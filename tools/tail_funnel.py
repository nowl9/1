"""Rolling reject-rate observability for Round 9c — operator tool.

Reads ``paper_ledger/rejections.jsonl`` periodically and prints per-reason
counts within each configured window (default 1h, 6h).  Surfaces regime
shifts mid-run (e.g. all rejections silently shifting to ``feed_stale``
between hours 12-47) without waiting for 9d post-hoc analysis.

CLI
---
::

    py -3.12 -m tools.tail_funnel \\
        --ledger-dir ./paper_ledger \\
        [--windows 1,6] [--refresh-s 30] [--arrow-threshold 1.5]

Output: one block per refresh tick; one line per rejection reason inside
each block, sorted by the first window's count descending::

    ── tail_funnel @ 2026-05-25T14:30:00+00:00 — 187 rejection(s) in last 6h
    kalshi_feed_stale   1h=42  6h=187  ←
    conservative_edge   1h=12  6h=85
    pm_spread           1h=3   6h=18

The arrow (``←``) marks reasons whose 1h count is materially above their
6h-extrapolated rate (``1h > arrow_threshold × (6h/6)``), with a minimum
6h count of ``_MIN_ARROW_6H`` to suppress small-sample noise.  Arrow
logic is only applied when both ``1`` and ``6`` are in ``--windows``.

Read-during-write safety: the agent writes ``rejections.jsonl`` in
append-only mode with ``fsync`` after each line (see ``paper_ledger.py``).
:meth:`PaperLedger.replay_rejections` handles the worst realistic case
(truncated trailing line) via skip-and-warn — the same path the
production reader uses on startup.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_pm_arb.execution.paper_ledger import PaperLedger, PaperRejectionRecord

# Arrow fires when 1h count > threshold × extrapolated-1h-from-6h.  Tuned
# at 1.5 for 9c — sensitive enough to catch regime shifts within an hour;
# loose enough to not fire on every minor up-tick.  Override via
# ``--arrow-threshold``.
_DEFAULT_ARROW_THRESHOLD: float = 1.5

# Minimum 6h count required before arrow logic is evaluated.  Prevents
# small-sample noise: with a 6h count of 3 the extrapolated 1h rate is
# 0.5, which any random 1h count of 1 trivially exceeds at threshold
# 1.5 — meaningless signal.
_MIN_ARROW_6H: int = 6


# ── CLI parsing ───────────────────────────────────────────────────────────────


def _parse_windows(s: str) -> list[float]:
    """Parse a comma-separated hours list into a list of positive floats."""
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("--windows must list at least one hour")
    out: list[float] = []
    for p in parts:
        try:
            v = float(p)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"--windows value {p!r} is not a number"
            ) from exc
        if v <= 0:
            raise argparse.ArgumentTypeError(
                f"--windows value {p!r} must be positive"
            )
        out.append(v)
    return out


# ── Pure rendering (testable without sleep / clock) ───────────────────────────


def render(
    records: list[PaperRejectionRecord],
    windows_hours: list[float],
    now: datetime,
    arrow_threshold: float = _DEFAULT_ARROW_THRESHOLD,
    min_arrow_6h: int = _MIN_ARROW_6H,
) -> str:
    """Render the per-reason × per-window count table for a snapshot.

    Pure function: no I/O, no clock reads.  Takes the records and the
    reference ``now`` as inputs.  Used by :func:`_refresh_once` for live
    operation and by tests directly.

    Returns one line per ``reason_key`` (sorted by the first window's
    count descending) plus an arrow marker per the regime-shift rule.
    Empty / no-data cases return a single sentinel line — never an
    empty string.
    """
    if not records:
        return "no rejections logged yet"

    # Window counts keyed by reason → {window_hours: count}.
    counts: dict[str, dict[float, int]] = {}
    for w in windows_hours:
        cutoff_w = now - timedelta(hours=w)
        for rec in records:
            if rec.timestamp >= cutoff_w:
                bucket = counts.setdefault(rec.reason_key, {})
                bucket[w] = bucket.get(w, 0) + 1

    if not counts:
        return "no rejections logged yet"

    # Arrow logic — only meaningful when both 1h and 6h are configured.
    arrow_enabled = 1.0 in windows_hours and 6.0 in windows_hours
    sort_window = windows_hours[0]
    rows = sorted(
        counts.items(),
        key=lambda kv: kv[1].get(sort_window, 0),
        reverse=True,
    )

    lines: list[str] = []
    for reason, by_w in rows:
        parts = [reason.ljust(28)]
        for w in windows_hours:
            n = by_w.get(w, 0)
            # Integer hours render without trailing decimals.
            w_label = str(int(w)) if w == int(w) else f"{w:g}"
            parts.append(f"{w_label}h={n}")
        if arrow_enabled:
            n1 = by_w.get(1.0, 0)
            n6 = by_w.get(6.0, 0)
            if n6 >= min_arrow_6h and n1 > arrow_threshold * (n6 / 6.0):
                parts.append("←")  # ←
        lines.append("  ".join(parts))

    return "\n".join(lines)


# ── Refresh loop ──────────────────────────────────────────────────────────────


def _snapshot_records(
    ledger: PaperLedger, cutoff: datetime
) -> list[PaperRejectionRecord]:
    """Load all rejections with ``timestamp >= cutoff``."""
    return [rec for rec in ledger.replay_rejections() if rec.timestamp >= cutoff]


def _refresh_once(
    ledger: PaperLedger,
    windows_hours: list[float],
    arrow_threshold: float,
    *,
    now: datetime | None = None,
) -> str:
    """One refresh cycle: snapshot records, render, return printable block.

    ``now`` defaults to ``datetime.now(timezone.utc)`` but is injectable
    so tests can drive a deterministic clock.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(windows_hours))
    records = _snapshot_records(ledger, cutoff)
    header = (
        f"── tail_funnel @ {now.isoformat(timespec='seconds')} "
        f"— {len(records)} rejection(s) in last {max(windows_hours):g}h"
    )
    return header + "\n" + render(records, windows_hours, now, arrow_threshold)


def main(argv: list[str] | None = None) -> int:
    # Windows console default codepage is cp1252; the table headers
    # ── and arrow ← are non-cp1252. Force utf-8 so operators on
    # PowerShell don't have to set PYTHONIOENCODING manually.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        prog="tail_funnel",
        description=(
            "Rolling reject-rate observability for 9c — refreshes every "
            "--refresh-s seconds against paper_ledger/rejections.jsonl."
        ),
    )
    parser.add_argument(
        "--ledger-dir", required=True,
        help="Path to the paper-trading ledger directory.",
    )
    parser.add_argument(
        "--windows", type=_parse_windows, default=_parse_windows("1,6"),
        help="Comma-separated window sizes in hours (default: 1,6).",
    )
    parser.add_argument(
        "--refresh-s", type=float, default=30.0,
        help="Seconds between refreshes (default: 30).",
    )
    parser.add_argument(
        "--arrow-threshold", type=float, default=_DEFAULT_ARROW_THRESHOLD,
        help=(
            "Arrow fires when 1h count > threshold * (6h count / 6) "
            f"(default: {_DEFAULT_ARROW_THRESHOLD})."
        ),
    )
    args = parser.parse_args(argv)

    ledger_dir = Path(args.ledger_dir)
    ledger = PaperLedger(ledger_dir)
    try:
        while True:
            print(_refresh_once(ledger, args.windows, args.arrow_threshold))
            print()  # blank line between refresh blocks
            sys.stdout.flush()
            time.sleep(args.refresh_s)
    except KeyboardInterrupt:
        print("\ntail_funnel: interrupted, exiting", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
