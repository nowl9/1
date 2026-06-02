"""Paper-ledger analysis tool — Round 9b1: load → join → parquet.

Reads ``orders.jsonl`` / ``fills.jsonl`` / ``settlements.jsonl`` from a
paper-trading ledger directory and produces a joined DataFrame that
9b2's statistical analyses + report rendering will consume.  This
commit deliberately stops at "structured data, ready for analysis."
9b2 layers logistic regression, bootstrap CIs, charts, and the
markdown report on top.

CLI
---
::

    py -3.12 -m tools.analyze_paper_ledger \\
        --ledger-dir ./paper_ledger \\
        --out-dir ./analysis_out \\
        [--as-of 2026-05-15T00:00:00Z]

Outputs ``analysis_out/joined.parquet`` plus a stdout summary
(record counts, schema-skip totals, power-tier label).

Schema-version contract
-----------------------
Records are validated against ``PaperOrderRecord`` /
``PaperFillRecord`` / ``PaperSettlementRecord`` from
``paper_ledger.py``.  The expected schema version is
``_SUPPORTED_SCHEMA_VERSION`` (currently 1).  Records with a
different ``schema_version`` field, malformed JSON, or pydantic
validation failures are skipped, counted, and logged at WARNING.
This mirrors the production ``PaperLedger`` reader's
"skip-and-warn-with-counter" policy but surfaces the per-record
skip reason in the analysis output rather than just an aggregate.

Decoupled from PaperLedger
--------------------------
The script reads the JSONL files directly (``json.loads`` +
``model_validate``) rather than via ``PaperLedger.replay_*()``.
Reasons:

1. The analysis cares which lines were skipped, not just how many —
   ``PaperLedger``'s aggregate counter loses the per-line context
   the analysis output wants to surface.
2. Decoupling means the analysis script keeps working if a future
   round changes ``PaperLedger``'s reader contract.

The pydantic record classes ARE imported (single source of truth for
the record schemas); only the I/O is independent.

vol_regime normalization
------------------------
Live ledger records serialize the enum as ``"VolRegime.LOW"``
(Python's default ``str()`` for mixed-class str-Enums under py 3.12).
This module normalizes on read into a ``vol_regime_clean`` column
(``"low"`` / ``"normal"`` / ``"high"``).  The original ``vol_regime``
field is preserved verbatim for forensics.

If a future commit cleans up the source-side serialization
(filters.py:389 → use ``.value``), the normalize-on-read remains
correct on already-clean values (idempotent prefix-strip + lowercase).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar

import pandas as pd
import structlog
from pydantic import BaseModel

from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperOrderRecord,
    PaperRejectionRecord,
    PaperSettlementRecord,
)
from tools.analysis.report import render_all

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Schema version this script understands.  Records with a different
# schema_version are skipped-and-warned; future migration tools handle
# version drift.  Mirrors paper_ledger.py:_SCHEMA_VERSION but kept local
# so a schema bump on the producer side fails loudly here rather than
# silently parsing forward-incompatible records.
_SUPPORTED_SCHEMA_VERSION: int = 1

# Conservative-edge bucket boundaries — left-inclusive, right-exclusive
# except the final bucket which is left-inclusive only ([0.10, ∞)).
# Edges below 1.0% map to ``"below_min"``; ``None`` maps to ``"unknown"``.
CONSERVATIVE_EDGE_BUCKETS: list[tuple[float, float, str]] = [
    (0.010, 0.015, "1.0-1.5%"),
    (0.015, 0.020, "1.5-2%"),
    (0.020, 0.030, "2-3%"),
    (0.030, 0.050, "3-5%"),
    (0.050, 0.100, "5-10%"),
    (0.100, float("inf"), "10%+"),
]

# Fill-adjusted-edge bucket boundaries — left-inclusive, right-exclusive.
# Unlike the conservative-edge bins (which start at the +1% floor) these span
# the negative axis: the whole point of book-walking the near-floor band is to
# expose theoretical edge collapsing through zero against thin near-mid depth,
# so the distribution must have somewhere for the negatives to land.
FILL_ADJUSTED_EDGE_BUCKETS: list[tuple[float, float, str]] = [
    (float("-inf"), -0.05, "<-5%"),
    (-0.05, -0.02, "-5to-2%"),
    (-0.02, 0.0, "-2to0%"),
    (0.0, 0.01, "0-1%"),
    (0.01, 0.03, "1-3%"),
    (0.03, 0.05, "3-5%"),
    (0.05, float("inf"), "5%+"),
]

# Power-tier gates (defined here, used by 9b2 — kept visible from the
# top of the script so 9c authors don't have to hunt for them).  The
# values come from the Round 9 plan's earlier statistical-power review:
# below tier, an analysis is "indicative, not calibration-quality."
TIER_SINGLE_THRESHOLD: int = 300
TIER_REGIME_CONDITIONAL: int = 800
TIER_MULTI_FEATURE: int = 1500


# ── LoadResult ───────────────────────────────────────────────────────────────

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class LoadResult(Generic[T]):
    """Result of loading one paper-ledger JSONL file.

    Attributes:
        records:                  Successfully-parsed records, in file order.
        n_skipped_unknown_schema: Lines skipped due to ``schema_version``
                                  mismatch with ``_SUPPORTED_SCHEMA_VERSION``.
        n_skipped_invalid:        Lines skipped due to JSON-parse or
                                  pydantic-validation failure.
    """

    records: list[T]
    n_skipped_unknown_schema: int
    n_skipped_invalid: int


# ── JSONL loader ─────────────────────────────────────────────────────────────

def load_jsonl(path: Path, model_cls: type[T]) -> LoadResult[T]:
    """Load one paper-ledger JSONL file with skip-and-warn handling.

    The function NEVER raises on bad data — corrupted lines (truncated
    on process kill, schema-version mismatches, pydantic validation
    failures) are logged at WARNING and counted.  The caller surfaces
    the skip totals in the user-facing summary so a degraded dataset
    is visible rather than silently smaller.

    Returns an empty ``LoadResult`` if the file does not exist (a fresh
    paper_ledger directory may have only some of the three files).
    """
    if not path.exists():
        return LoadResult(records=[], n_skipped_unknown_schema=0, n_skipped_invalid=0)
    records: list[T] = []
    n_unknown = 0
    n_invalid = 0
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if not stripped:
                # Blank lines are silently skipped (matches paper_ledger.py
                # reader policy — abrupt-truncation tolerance).
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                n_invalid += 1
                logger.warning(
                    "analyze.parse_error",
                    path=str(path),
                    lineno=lineno,
                    error=str(exc),
                )
                continue
            schema = payload.get("schema_version")
            if schema != _SUPPORTED_SCHEMA_VERSION:
                n_unknown += 1
                logger.warning(
                    "analyze.unknown_schema_version",
                    path=str(path),
                    lineno=lineno,
                    schema_version=schema,
                    expected=_SUPPORTED_SCHEMA_VERSION,
                )
                continue
            try:
                record = model_cls.model_validate(payload)
            except Exception as exc:
                n_invalid += 1
                logger.warning(
                    "analyze.validation_error",
                    path=str(path),
                    lineno=lineno,
                    error=str(exc),
                )
                continue
            records.append(record)
    return LoadResult(
        records=records,
        n_skipped_unknown_schema=n_unknown,
        n_skipped_invalid=n_invalid,
    )


# ── Normalizers ──────────────────────────────────────────────────────────────

def normalize_vol_regime(raw: str | None) -> str:
    """Normalize a ledger ``vol_regime`` field to a clean lowercase label.

    Idempotent on already-clean values, so a future producer-side
    cleanup that emits ``"low"`` / ``"normal"`` / ``"high"`` directly
    will work without changes here.

    Examples::

        normalize_vol_regime("VolRegime.LOW")    → "low"
        normalize_vol_regime("low")              → "low"
        normalize_vol_regime("VolRegime.HIGH")   → "high"
        normalize_vol_regime(None)               → "unknown"
    """
    if raw is None:
        return "unknown"
    s = raw
    if s.startswith("VolRegime."):
        s = s[len("VolRegime."):]
    return s.lower()


def compute_max_feed_staleness(staleness_dict: dict | None) -> float | None:
    """Return the max staleness across all feeds, ignoring ``None`` values.

    The on-disk shape is ``dict[str, float | None]`` per the
    ``PaperOrderRecord.feed_staleness_ms`` contract (paper_ledger.py:172).
    Returns ``None`` when the dict is missing/empty or every entry is
    ``None`` — distinct from "0.0 ms" so downstream bucketing can tell
    "no measurement" apart from "fresh feeds."
    """
    if not staleness_dict:
        return None
    valid = [v for v in staleness_dict.values() if v is not None]
    if not valid:
        return None
    return max(valid)


def assign_conservative_edge_bucket(value: float | None) -> str:
    """Assign an edge value to a fixed-bin label.

    Buckets are left-inclusive, right-exclusive except the final bucket
    which is left-inclusive only.  Values below 1.0% (the Round 9a
    pipeline-noise floor) return ``"below_min"``; ``None`` returns
    ``"unknown"`` so the bucketing surface always returns a label.
    """
    if value is None:
        return "unknown"
    if value < 0.010:
        return "below_min"
    for low, high, label in CONSERVATIVE_EDGE_BUCKETS:
        if low <= value < high:
            return label
    # Unreachable: the final bucket has high=inf.
    return "unknown"


def assign_fill_adjusted_edge_bucket(value: float | None) -> str:
    """Assign a fill-adjusted edge to a signed fixed-bin label.

    Buckets are left-inclusive, right-exclusive and span the negative axis (see
    ``FILL_ADJUSTED_EDGE_BUCKETS``).  ``None`` returns ``"no_fill"`` — a walk
    that produced no fill price (empty / limit-below book) has no edge to
    bucket, distinct from a fill that landed at exactly 0%.
    """
    if value is None:
        return "no_fill"
    for low, high, label in FILL_ADJUSTED_EDGE_BUCKETS:
        if low <= value < high:
            return label
    # Unreachable: the final bucket has high=inf.
    return "no_fill"


def fill_adjusted_band_distribution(
    rejections: list[PaperRejectionRecord],
) -> dict[str, Any]:
    """Summarise the book-walked fill-adjusted edge across the near-floor band.

    This is the rejection-side counterpart to the passer calibration report:
    the near-floor [1-3%) band previously existed only as un-book-walked
    rejections, so a fill-adjusted distribution that spans the band (not just
    the 1-2 contracts that cleared the floor) had nowhere to come from.

    Only rejections that were actually book-walked (``fill_outcome is not
    None``) contribute.  full / partial walks carry a ``fill_adjusted_edge`` and
    land in a signed band bucket; no_fill walks (empty / limit-below book) are
    counted in the ``"no_fill"`` bucket (no edge to place).  Each bucket also
    reports the mean theoretical edge (``best_conservative_edge``) so the
    collapse from theoretical to fill-adjusted is visible side by side.
    """
    order = [label for _, _, label in FILL_ADJUSTED_EDGE_BUCKETS] + ["no_fill"]
    grouped: dict[str, list[PaperRejectionRecord]] = {label: [] for label in order}

    walked = [r for r in rejections if r.fill_outcome is not None]
    for r in walked:
        grouped[assign_fill_adjusted_edge_bucket(r.fill_adjusted_edge)].append(r)

    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    buckets: list[dict[str, Any]] = []
    for label in order:
        members = grouped[label]
        faes = [m.fill_adjusted_edge for m in members if m.fill_adjusted_edge is not None]
        buckets.append({
            "bucket": label,
            "n": len(members),
            "n_full": sum(1 for m in members if m.fill_outcome == "full"),
            "n_partial": sum(1 for m in members if m.fill_outcome == "partial"),
            "mean_fill_adjusted_edge": _mean(faes),
            "mean_best_conservative_edge": _mean([m.best_conservative_edge for m in members]),
        })

    return {
        "boundaries": [
            [("-inf" if low == float("-inf") else low),
             ("inf" if high == float("inf") else high), label]
            for low, high, label in FILL_ADJUSTED_EDGE_BUCKETS
        ],
        "n_rejections_total": len(rejections),
        "n_walked": len(walked),
        "n_full": sum(1 for r in walked if r.fill_outcome == "full"),
        "n_partial": sum(1 for r in walked if r.fill_outcome == "partial"),
        "n_no_fill": sum(1 for r in walked if r.fill_outcome == "no_fill"),
        "n_skipped": len(rejections) - len(walked),
        "buckets": buckets,
    }


# ── DataFrame join ───────────────────────────────────────────────────────────

_EMPTY_DERIVED_COLUMNS: list[str] = [
    "client_order_id",
    "is_settled",
    "vol_regime_clean",
    "max_feed_staleness_ms",
    "conservative_edge_bucket",
    "slippage",
    "return",
]


def build_joined_dataframe(
    orders: list[PaperOrderRecord],
    fills: list[PaperFillRecord],
    settlements: list[PaperSettlementRecord],
) -> pd.DataFrame:
    """Three-way join: orders ⨝ fills ⨝ settlements on ``client_order_id``.

    Produces one row per order with NaN-padded settlement columns for
    unsettled orders.  Adds derived columns:

      - ``is_settled``                bool — True iff a matching settlement row exists
      - ``vol_regime_clean``          normalized vol regime ("low"/"normal"/"high")
      - ``max_feed_staleness_ms``     max across the per-feed staleness dict
      - ``conservative_edge_bucket``  fixed-bin label
      - ``slippage``                  fill_price - limit_price (fill-fidelity;
                                      signed, negative = price improvement),
                                      NaN when there is no fill price
      - ``return``                    realized_pnl / size_usd, NaN when not settled

    Fill-fidelity (build step 4): ``slippage`` joins the simulated
    ``fill_price`` against the intended ``limit_price`` per
    ``client_order_id``; ``realized_pnl`` / ``return`` carry the settled
    P&L.  ``run_id`` / ``mode`` ride through from the records so runs are
    separable in the parquet.

    Merge mechanics: pandas left-merge with default suffixing — only
    colliding columns get the suffix.  ``fill_price`` and
    ``fill_outcome`` (which start with "fill_") stay un-suffixed
    because no order column collides; ``fees_usd`` (on both fills and
    settlements) becomes ``fees_usd`` (from fills) and
    ``fees_usd_settle`` (from the second merge).
    """
    if not orders:
        # Empty input → return an empty frame with the expected derived
        # columns so downstream code can address them safely.
        return pd.DataFrame(columns=_EMPTY_DERIVED_COLUMNS)

    orders_df = pd.DataFrame([r.model_dump(mode="json") for r in orders])
    fills_df = (
        pd.DataFrame([r.model_dump(mode="json") for r in fills])
        if fills
        else pd.DataFrame()
    )
    settlements_df = (
        pd.DataFrame([r.model_dump(mode="json") for r in settlements])
        if settlements
        else pd.DataFrame()
    )

    merged = orders_df.copy()
    if not fills_df.empty:
        merged = merged.merge(
            fills_df, on="client_order_id", how="left", suffixes=("", "_fill"),
        )
    if not settlements_df.empty:
        merged = merged.merge(
            settlements_df, on="client_order_id", how="left", suffixes=("", "_settle"),
        )

    # Derived: is_settled ─ True iff the settlements merge produced a row
    # (settled_at NaN ↔ no settlement matched).
    if "settled_at" in merged.columns:
        merged["is_settled"] = merged["settled_at"].notna()
    else:
        merged["is_settled"] = False

    # Derived: vol_regime_clean
    merged["vol_regime_clean"] = merged["vol_regime"].map(normalize_vol_regime)

    # Derived: max_feed_staleness_ms (single scalar per row)
    merged["max_feed_staleness_ms"] = merged["feed_staleness_ms"].map(
        compute_max_feed_staleness
    )

    # Derived: per-feed staleness scalars.  Replaces the nested
    # ``feed_staleness_ms`` dict column with three flat ``<feed>_staleness_ms``
    # columns — both for parquet-safety (object-dtype dicts can confuse
    # pyarrow's schema inference when key sets vary across rows) and so
    # 9b2's per-feed analysis can read them directly.
    for feed in ("deribit", "kalshi", "polymarket"):
        merged[f"{feed}_staleness_ms"] = merged["feed_staleness_ms"].map(
            lambda d, _feed=feed: (
                d.get(_feed) if isinstance(d, dict) else None
            )
        )

    # Derived: conservative_edge_bucket — adjusted_edge is the
    # post-spread, post-basis-adjustment edge from the
    # Round-9a-and-earlier FilterConfig contract.
    merged["conservative_edge_bucket"] = merged["adjusted_edge"].map(
        assign_conservative_edge_bucket
    )

    # Derived: slippage = fill_price - limit_price (fill-fidelity, build
    # step 4).  Signed: negative = the book-walk filled better than the
    # intended limit; positive = worse.  NaN when there is no fill price
    # (no_fill or a missing fill row).  fill_price comes from the fills
    # merge un-suffixed (no order column collides); limit_price is the
    # order's intended price.
    if "fill_price" in merged.columns and "limit_price" in merged.columns:
        merged["slippage"] = merged["fill_price"] - merged["limit_price"]
    else:
        merged["slippage"] = float("nan")

    # Derived: return = realized_pnl / size_usd; NaN when not settled.
    if "realized_pnl" in merged.columns:
        merged["return"] = merged["realized_pnl"] / merged["size_usd"]
    else:
        merged["return"] = float("nan")

    # Drop heavy nested columns the analysis doesn't consume — keeps
    # the parquet output flat and small.  ``feed_staleness_ms`` is now
    # captured by the per-feed and max-staleness scalars above;
    # ``order_book_yes`` / ``order_book_no`` aren't used in 9b's
    # analysis (a future round that wants depth analysis would need
    # to re-read the JSONL or persist them as serialized JSON).
    for col in ("feed_staleness_ms", "order_book_yes", "order_book_no"):
        if col in merged.columns:
            merged = merged.drop(columns=[col])

    return merged


# ── as-of filter ─────────────────────────────────────────────────────────────

def filter_as_of(df: pd.DataFrame, as_of: datetime | None) -> pd.DataFrame:
    """Restrict the joined frame to records as they would have looked at ``as_of``.

    No-op when ``as_of`` is ``None``.  Two effects:

    1. Orders ``created_at > as_of`` are dropped entirely.
    2. Settlements ``settled_at > as_of`` are zeroed out — the order
       row remains, but ``is_settled`` flips to ``False`` and every
       ``settle_*`` / ``realized_pnl`` / ``return`` column is nulled
       so the analysis sees the order as still-open at ``as_of``.

    This lets us re-run point-in-time analyses (e.g., "what would
    calibration have looked like after 24 h?") without re-collecting data.
    """
    if as_of is None or df.empty:
        return df
    created = pd.to_datetime(df["created_at"], utc=True)
    filtered = df[created <= as_of].copy()
    if "settled_at" in filtered.columns:
        settled = pd.to_datetime(
            filtered["settled_at"], utc=True, errors="coerce",
        )
        post_as_of = settled > as_of
        # Null every settlement-side column for late settlements so the
        # frame looks as it would have at as_of.  Catches both renamed
        # collision columns (``fees_usd_settle``) and the rest.
        settle_cols = [
            c for c in filtered.columns
            if c == "settled_at"
            or c.endswith("_settle")
            or c in {
                "settlement_price", "payout_price", "entry_price",
                "realized_pnl", "outcome", "theoretical_edge",
            }
        ]
        for col in settle_cols:
            filtered.loc[post_as_of, col] = None
        filtered.loc[post_as_of, "is_settled"] = False
        filtered.loc[post_as_of, "return"] = float("nan")
    return filtered


# ── Power-tier assessment ────────────────────────────────────────────────────

def assess_power_tier(n_settled: int) -> str:
    """Return the highest power tier reached for a given ``N_settled``.

    Returns one of ``"below_single_threshold"`` /
    ``"single_threshold"`` / ``"regime_conditional"`` /
    ``"multi_feature"``.  Used by 9b2 to gate which model classes
    the calibration analysis is allowed to recommend.
    """
    if n_settled >= TIER_MULTI_FEATURE:
        return "multi_feature"
    if n_settled >= TIER_REGIME_CONDITIONAL:
        return "regime_conditional"
    if n_settled >= TIER_SINGLE_THRESHOLD:
        return "single_threshold"
    return "below_single_threshold"


# ── Run-id scoping ─────────────────────────────────────────────────────────────

def latest_run_id(orders: list[PaperOrderRecord]) -> str | None:
    """Return the ``run_id`` of the most-recently-created order, or None.

    "Most recent" = max ``created_at``.  Used as the default analysis scope so
    a ledger dir accumulating many runs reports on the freshest run rather than
    silently aggregating month-old phantom rows from earlier runs (the
    observed failure: 137 stale rows, 0 from today's run_id).  Pre-step-4
    legacy rows carry ``run_id == ""`` and a pure-legacy ledger therefore still
    scopes to "" (a no-op), while a fresh uuid-stamped run is auto-isolated.
    """
    if not orders:
        return None
    return max(orders, key=lambda r: r.created_at).run_id


# ── Main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI entry point — load → join → write parquet → print summary."""
    parser = argparse.ArgumentParser(
        description=(
            "Analyze a paper-trading ledger.  Round 9b1: load → join → parquet."
        ),
    )
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=Path("./paper_ledger"),
        help="Path to the paper-ledger directory (default: ./paper_ledger).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./analysis_out"),
        help="Output directory; created if missing (default: ./analysis_out).",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help=(
            "ISO 8601 timestamp for point-in-time analysis "
            "(default: no filter).  Naive timestamps are interpreted as UTC."
        ),
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help=(
            "Restrict analysis to one run_id.  Default: the latest run (by "
            "order created_at), so stale phantom rows from earlier runs in the "
            "same ledger dir are excluded.  Pass 'all' to analyze every run."
        ),
    )
    args = parser.parse_args(argv)

    as_of: datetime | None = None
    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    orders_result = load_jsonl(
        args.ledger_dir / "orders.jsonl", PaperOrderRecord,
    )
    fills_result = load_jsonl(
        args.ledger_dir / "fills.jsonl", PaperFillRecord,
    )
    settlements_result = load_jsonl(
        args.ledger_dir / "settlements.jsonl", PaperSettlementRecord,
    )
    rejections_result = load_jsonl(
        args.ledger_dir / "rejections.jsonl", PaperRejectionRecord,
    )

    # ── Run-id scoping (C3b) ────────────────────────────────────────────────
    # Default to the latest run so the analyzer stops aggregating month-old
    # phantom rows; --run-id pins an explicit run; --run-id all opts out.
    if args.run_id == "all":
        target_run_id: str | None = None
    elif args.run_id is not None:
        target_run_id = args.run_id
    else:
        target_run_id = latest_run_id(orders_result.records)

    if target_run_id is None:
        orders_records = orders_result.records
        fills_records = fills_result.records
        settlements_records = settlements_result.records
        rejections_records = rejections_result.records
        scope_label = "all runs"
    else:
        orders_records = [r for r in orders_result.records if r.run_id == target_run_id]
        fills_records = [r for r in fills_result.records if r.run_id == target_run_id]
        settlements_records = [
            r for r in settlements_result.records if r.run_id == target_run_id
        ]
        rejections_records = [
            r for r in rejections_result.records if r.run_id == target_run_id
        ]
        scope_label = f"run_id={target_run_id!r}"
    print(
        f"Scope: {scope_label} "
        f"({len(orders_records)}/{len(orders_result.records)} orders in scope)."
    )

    df = build_joined_dataframe(
        orders_records,
        fills_records,
        settlements_records,
    )
    df = filter_as_of(df, as_of)

    parquet_path = args.out_dir / "joined.parquet"
    df.to_parquet(parquet_path, index=False)

    n_orders = len(df)
    n_settled = int(df["is_settled"].sum()) if not df.empty else 0
    n_open = n_orders - n_settled
    tier = assess_power_tier(n_settled)

    print(
        f"Loaded {len(orders_result.records)} orders, "
        f"{len(fills_result.records)} fills, "
        f"{len(settlements_result.records)} settlements."
    )
    print(f"Joined: {n_orders} orders ({n_settled} settled, {n_open} open).")
    print(
        f"Schema-version skips: orders={orders_result.n_skipped_unknown_schema}, "
        f"fills={fills_result.n_skipped_unknown_schema}, "
        f"settlements={settlements_result.n_skipped_unknown_schema}."
    )
    print(
        f"Invalid-record skips: orders={orders_result.n_skipped_invalid}, "
        f"fills={fills_result.n_skipped_invalid}, "
        f"settlements={settlements_result.n_skipped_invalid}."
    )
    print(f"Power tier: {tier} (N_settled={n_settled}).")
    print(f"Wrote {parquet_path}.")

    # ── 9b2b: hand off to the analysis pipeline ─────────────────────────────
    # Re-presents the existing skip counters in the shape render_all
    # accepts; both inner keys mirror the stdout summary above.  Errors
    # propagate by design — a half-rendered out_dir (some PNGs, no
    # report.md) is worse than a CLI traceback the operator can act on.
    schema_skips: dict[str, dict[str, int]] = {
        "orders": {
            "unknown_schema": orders_result.n_skipped_unknown_schema,
            "invalid": orders_result.n_skipped_invalid,
        },
        "fills": {
            "unknown_schema": fills_result.n_skipped_unknown_schema,
            "invalid": fills_result.n_skipped_invalid,
        },
        "settlements": {
            "unknown_schema": settlements_result.n_skipped_unknown_schema,
            "invalid": settlements_result.n_skipped_invalid,
        },
    }
    render_all(
        df,
        args.out_dir,
        schema_skips=schema_skips,
        as_of=as_of,
        ledger_dir=str(args.ledger_dir),
    )
    print(f"Wrote {args.out_dir / 'report.md'}.")
    print(f"Wrote {args.out_dir / 'summary_stats.json'}.")
    print(f"Wrote {args.out_dir / 'charts'}/ (21 PNGs).")

    # ── Fill-adjusted band distribution (rejection-side measurement infra) ───
    # The passer calibration report above sees only orders that cleared the
    # floor.  This sidecar emits the book-walked fill-adjusted edge across the
    # near-floor band from rejections.jsonl — the [1-3%) band that previously
    # existed only as un-book-walked rejections — so the distribution spans the
    # band, not just passers.  Written as a separate JSON (not folded into
    # summary_stats.json) so the passer-calibration schema stays untouched.
    band = fill_adjusted_band_distribution(rejections_records)
    band_path = args.out_dir / "fill_adjusted_band.json"
    band_path.write_text(json.dumps(band, indent=2), encoding="utf-8")
    print(
        f"Rejections: {band['n_rejections_total']} total "
        f"({len(rejections_records)}/{len(rejections_result.records)} in scope), "
        f"{band['n_walked']} book-walked "
        f"({band['n_full']} full, {band['n_partial']} partial, "
        f"{band['n_no_fill']} no_fill, {band['n_skipped']} skipped)."
    )
    if band["n_walked"]:
        print("Fill-adjusted edge band (book-walked near-floor rejections):")
        print("  bucket        n   mean_fill_adj  mean_theoretical")
        for row in band["buckets"]:
            if row["n"] == 0:
                continue
            mfae = row["mean_fill_adjusted_edge"]
            mbce = row["mean_best_conservative_edge"]
            mfae_s = f"{mfae:+.4f}" if mfae is not None else "     n/a"
            mbce_s = f"{mbce:+.4f}" if mbce is not None else "     n/a"
            print(f"  {row['bucket']:<10} {row['n']:>4}   {mfae_s:>10}   {mbce_s:>10}")
    print(f"Wrote {band_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
