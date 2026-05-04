"""Report rendering — markdown text + summary_stats.json + chart bundle.

Public entry point: :func:`render_all`.  Takes a joined DataFrame plus
metadata (schema-skip counts, as-of timestamp, ledger dir) and writes
``report.md``, ``charts/*.png``, and ``summary_stats.json`` into the
output directory.

Returns the in-memory ``summary_stats`` dict so library-direct tests
can assert against the structure without re-reading the JSON file.

Tier and bucket-ordering decisions
----------------------------------
The five univariate dimensions render in this fixed order, mirroring
the report sections:

    conservative_edge → vol_regime → match_quality
    → max_feed_staleness_ms → confidence

The conservative-edge bucket order is the fixed-bin order from
``CONSERVATIVE_EDGE_BUCKETS`` so chart x-axis ordering is stable
across runs; vol_regime is ``["low", "normal", "high"]``; quartile
dimensions render in q1→q4 order.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tools.analysis import charts as charts_mod
from tools.analysis import stats as stats_mod
from tools.analysis.stats import (
    BucketSummary,
    LogitResult,
    Recommendation,
    TIER_MULTI_FEATURE,
    TIER_REGIME_CONDITIONAL,
    TIER_SINGLE_THRESHOLD,
    assign_quartile_buckets,
    bucket_summary,
    fit_logit,
    generate_recommendations,
    open_positions_snapshot,
)

# Mirrors the bin order in CONSERVATIVE_EDGE_BUCKETS plus the two
# special labels the bucketer can emit ("below_min", "unknown").  Drives
# both the markdown table row order and the chart x-axis ordering.
_EDGE_BUCKET_ORDER: list[str] = [
    "below_min",
    "1.0-1.5%",
    "1.5-2%",
    "2-3%",
    "3-5%",
    "5-10%",
    "10%+",
    "unknown",
]
_VOL_REGIME_ORDER: list[str] = ["low", "normal", "high", "unknown"]
_QUARTILE_ORDER: list[str] = ["q1", "q2", "q3", "q4"]

_REPORT_FILENAME: str = "report.md"
_SUMMARY_FILENAME: str = "summary_stats.json"
_CHARTS_SUBDIR: str = "charts"

_SUMMARY_SCHEMA_VERSION: int = 1


# ── Public entry point ───────────────────────────────────────────────────────


def render_all(
    df: pd.DataFrame,
    out_dir: Path,
    *,
    schema_skips: dict[str, dict[str, int]] | None = None,
    as_of: datetime | None = None,
    ledger_dir: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full analysis pipeline against ``df`` and write outputs.

    Writes three deliverables to ``out_dir``:

    * ``report.md``  — narrative + tables + chart references
    * ``charts/*.png`` — 21 figures at 150 DPI
    * ``summary_stats.json`` — machine-readable rollup for 9c

    Returns the in-memory summary dict so callers (and tests) can
    inspect it without re-reading the file.

    ``now`` is injectable for deterministic ``generated_at`` in tests.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    charts_dir = out_dir / _CHARTS_SUBDIR
    charts_dir.mkdir(parents=True, exist_ok=True)
    if now is None:
        now = datetime.now(timezone.utc)

    # ── 1. Compute all the per-dimension bucket summaries ────────────────────
    edge_buckets = bucket_summary(
        df, "conservative_edge_bucket", bucket_order=_EDGE_BUCKET_ORDER,
    )
    vol_buckets = bucket_summary(
        df, "vol_regime_clean", bucket_order=_VOL_REGIME_ORDER,
    )

    # Quartile dims: assign buckets into a temp column on a copy, then
    # summarize.  Boundaries are recorded in summary_stats.json.
    df_quart = df.copy()
    quartile_meta: dict[str, list[float]] = {}
    for col in ("match_quality", "max_feed_staleness_ms", "confidence"):
        labels, bins = assign_quartile_buckets(df_quart, col)
        df_quart[f"_{col}_bucket"] = labels
        quartile_meta[col] = bins
    match_buckets = bucket_summary(
        df_quart, "_match_quality_bucket", bucket_order=_QUARTILE_ORDER,
    )
    staleness_buckets = bucket_summary(
        df_quart, "_max_feed_staleness_ms_bucket", bucket_order=_QUARTILE_ORDER,
    )
    confidence_buckets = bucket_summary(
        df_quart, "_confidence_bucket", bucket_order=_QUARTILE_ORDER,
    )

    # ── 2. Logit (always attempt; gating expressed in quality_flag) ─────────
    logit = fit_logit(df)

    # ── 3. Top-line stats ───────────────────────────────────────────────────
    counts = _compute_counts(df)
    top_line = _compute_top_line_stats(df)
    open_snap = open_positions_snapshot(df)

    # ── 4. Regime × edge crosstab (gated) ───────────────────────────────────
    n_settled = counts["n_settled"]
    if n_settled >= TIER_REGIME_CONDITIONAL:
        crosstab = _regime_x_edge_crosstab(df)
    else:
        crosstab = {
            "gated": True,
            "tier_required": "regime_conditional",
            "n_settled": n_settled,
            "n_required": TIER_REGIME_CONDITIONAL,
            "rows": None,
        }

    # ── 5. Recommendations (single-rule, gated to calibration buckets) ──────
    recommendations = generate_recommendations(edge_buckets)

    # ── 6. Tier label + dataset window ──────────────────────────────────────
    tier_reached = _assess_tier(n_settled)
    window = _dataset_window(df, as_of=as_of, generated_at=now)

    # ── 7. Build the summary_stats dict ─────────────────────────────────────
    summary: dict[str, Any] = {
        "schema_version": _SUMMARY_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "ledger_dir": ledger_dir,
        "dataset_window": window,
        "counts": counts,
        "tier_reached": tier_reached,
        "schema_skips": schema_skips or {},
        "top_line_stats": top_line,
        "bucket_summaries": {
            "conservative_edge": {
                "bucket_kind": "fixed_bins",
                "boundaries": _edge_bucket_boundaries(),
                "rows": [dataclasses.asdict(b) for b in edge_buckets],
            },
            "vol_regime_clean": {
                "bucket_kind": "categorical",
                "boundaries": _VOL_REGIME_ORDER,
                "rows": [dataclasses.asdict(b) for b in vol_buckets],
            },
            "match_quality": {
                "bucket_kind": "quartile",
                "boundaries": quartile_meta["match_quality"],
                "rows": [dataclasses.asdict(b) for b in match_buckets],
            },
            "max_feed_staleness_ms": {
                "bucket_kind": "quartile",
                "boundaries": quartile_meta["max_feed_staleness_ms"],
                "rows": [dataclasses.asdict(b) for b in staleness_buckets],
            },
            "confidence": {
                "bucket_kind": "quartile",
                "boundaries": quartile_meta["confidence"],
                "rows": [dataclasses.asdict(b) for b in confidence_buckets],
            },
        },
        "regime_x_edge_crosstab": crosstab,
        "logit_coefficients": _serialize_logit(logit),
        "open_positions": open_snap,
        "recommendations": [dataclasses.asdict(r) for r in recommendations],
    }

    # ── 8. Render charts ────────────────────────────────────────────────────
    _render_all_charts(
        df, charts_dir,
        edge_buckets=edge_buckets,
        vol_buckets=vol_buckets,
        match_buckets=match_buckets,
        staleness_buckets=staleness_buckets,
        confidence_buckets=confidence_buckets,
        logit=logit,
    )

    # ── 9. Write summary_stats.json + report.md ─────────────────────────────
    (out_dir / _SUMMARY_FILENAME).write_text(
        json.dumps(summary, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / _REPORT_FILENAME).write_text(
        _render_markdown(
            summary,
            edge_buckets=edge_buckets,
            vol_buckets=vol_buckets,
            match_buckets=match_buckets,
            staleness_buckets=staleness_buckets,
            confidence_buckets=confidence_buckets,
            logit=logit,
            recommendations=recommendations,
        ),
        encoding="utf-8",
    )

    return summary


# ── Helpers: stats glue ──────────────────────────────────────────────────────


def _compute_counts(df: pd.DataFrame) -> dict[str, int]:
    if df.empty:
        return {
            "n_orders": 0, "n_settled": 0, "n_open": 0,
            "n_wins": 0, "n_losses": 0, "n_pushes": 0,
        }
    n_orders = int(len(df))
    settled_mask = df["is_settled"].fillna(False).astype(bool)
    n_settled = int(settled_mask.sum())
    n_open = n_orders - n_settled
    if "outcome" in df.columns:
        outcomes = df.loc[settled_mask, "outcome"]
        n_wins = int((outcomes == "win").sum())
        n_losses = int((outcomes == "loss").sum())
        n_pushes = int((outcomes == "push").sum())
    else:
        n_wins = n_losses = n_pushes = 0
    return {
        "n_orders": n_orders,
        "n_settled": n_settled,
        "n_open": n_open,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "n_pushes": n_pushes,
    }


def _compute_top_line_stats(df: pd.DataFrame) -> dict[str, float | None]:
    if df.empty:
        return {
            "overall_win_rate": None,
            "mean_realized_pnl": None,
            "mean_return": None,
            "overall_honesty_ratio": None,
        }
    settled = df[df["is_settled"].fillna(False).astype(bool)]
    eligible = (
        settled[settled["outcome"].isin(["win", "loss"])]
        if "outcome" in settled.columns
        else settled.iloc[0:0]
    )
    if eligible.empty:
        return {
            "overall_win_rate": None,
            "mean_realized_pnl": None,
            "mean_return": None,
            "overall_honesty_ratio": None,
        }
    win_rate = float((eligible["outcome"] == "win").mean())
    realized = pd.to_numeric(eligible.get("realized_pnl", pd.Series(dtype=float)), errors="coerce")
    returns = pd.to_numeric(eligible["return"], errors="coerce")
    te = pd.to_numeric(eligible.get("theoretical_edge", pd.Series(dtype=float)), errors="coerce")
    h = stats_mod.honesty_ratio(returns.to_numpy(), te.to_numpy())
    return {
        "overall_win_rate": win_rate,
        "mean_realized_pnl": float(realized.mean()) if realized.notna().any() else None,
        "mean_return": float(returns.mean()) if returns.notna().any() else None,
        "overall_honesty_ratio": h,
    }


def _regime_x_edge_crosstab(df: pd.DataFrame) -> dict[str, Any]:
    """Cell-level mean return + win rate for vol_regime × edge bucket.

    Only called when n_settled >= TIER_REGIME_CONDITIONAL (gating
    handled by the caller).  Returns a flat row list rather than a
    nested dict so the JSON payload is straightforward to consume.
    """
    settled = df[df["is_settled"].fillna(False).astype(bool)]
    eligible = settled[settled["outcome"].isin(["win", "loss"])]
    rows: list[dict[str, Any]] = []
    if eligible.empty:
        return {"gated": False, "rows": rows}
    grouped = eligible.groupby(["vol_regime_clean", "conservative_edge_bucket"])
    for (regime, bucket), sub in grouped:
        rows.append({
            "vol_regime_clean": str(regime),
            "conservative_edge_bucket": str(bucket),
            "n": int(len(sub)),
            "win_rate": float((sub["outcome"] == "win").mean()),
            "mean_return": float(pd.to_numeric(sub["return"], errors="coerce").mean()),
        })
    return {"gated": False, "rows": rows}


def _serialize_logit(logit: LogitResult) -> dict[str, Any]:
    """Convert a LogitResult into the JSON-shaped sub-dict.

    Note: ``quality_flag`` is a structured field in the JSON, not just a
    report label — 9c's reader needs to dispatch on it programmatically.
    """
    if not logit.fitted:
        return {
            "fitted": False,
            "reason": logit.reason,
            "n": logit.n,
            "quality_flag": "indicative",
        }
    return {
        "fitted": True,
        "n_observations": logit.n,
        "reference_vol_regime": logit.reference_vol_regime,
        "mcfaddens_r2": logit.mcfaddens_r2,
        "converged": logit.converged,
        "confusion_matrix_at_05": logit.confusion_matrix_at_05,
        "features": [dataclasses.asdict(f) for f in (logit.features or [])],
        "quality_flag": logit.quality_flag,
    }


def _assess_tier(n_settled: int) -> str:
    if n_settled >= TIER_MULTI_FEATURE:
        return "multi_feature"
    if n_settled >= TIER_REGIME_CONDITIONAL:
        return "regime_conditional"
    if n_settled >= TIER_SINGLE_THRESHOLD:
        return "single_threshold"
    return "below_single_threshold"


def _dataset_window(
    df: pd.DataFrame, *, as_of: datetime | None, generated_at: datetime,
) -> dict[str, str | None]:
    if df.empty or "created_at" not in df.columns:
        return {
            "earliest_order_at": None,
            "latest_order_at": None,
            "as_of": as_of.isoformat() if as_of else None,
            "generated_at": generated_at.isoformat(),
        }
    created = pd.to_datetime(df["created_at"], utc=True, errors="coerce").dropna()
    return {
        "earliest_order_at": created.min().isoformat() if not created.empty else None,
        "latest_order_at": created.max().isoformat() if not created.empty else None,
        "as_of": as_of.isoformat() if as_of else None,
        "generated_at": generated_at.isoformat(),
    }


def _edge_bucket_boundaries() -> list[list[Any]]:
    """Return the conservative-edge bucket boundaries for the JSON blob.

    Rendered as nested lists (not tuples) so JSON serialization is
    straightforward.  inf upper bound is rendered as the string "inf"
    rather than the float — the JSON spec doesn't allow inf.
    """
    from tools.analyze_paper_ledger import CONSERVATIVE_EDGE_BUCKETS
    out: list[list[Any]] = []
    for low, high, label in CONSERVATIVE_EDGE_BUCKETS:
        upper: Any = "inf" if high == float("inf") else high
        out.append([low, upper, label])
    return out


# ── Charts orchestration ─────────────────────────────────────────────────────


def _render_all_charts(
    df: pd.DataFrame,
    charts_dir: Path,
    *,
    edge_buckets: list[BucketSummary],
    vol_buckets: list[BucketSummary],
    match_buckets: list[BucketSummary],
    staleness_buckets: list[BucketSummary],
    confidence_buckets: list[BucketSummary],
    logit: LogitResult,
) -> None:
    """Render the 20 univariate charts + 1 logit forest plot.

    Filename convention: ``<dimension>__<chart_kind>.png``.  The
    duplication across the four chart kinds is intentional — it
    sacrifices a few lines of orchestration for completely unsurprising
    filenames a reader can grep for.
    """
    dims: list[tuple[str, str, str, list[BucketSummary]]] = [
        ("conservative_edge", "Conservative edge", "adjusted_edge", edge_buckets),
        ("vol_regime_clean", "Vol regime", "vol_regime_clean", vol_buckets),
        ("match_quality", "Match quality", "match_quality", match_buckets),
        ("max_feed_staleness_ms", "Max feed staleness (ms)", "max_feed_staleness_ms", staleness_buckets),
        ("confidence", "Confidence", "confidence", confidence_buckets),
    ]

    for slug, pretty, source_col, buckets in dims:
        # Histogram dispatch: numeric columns get a true histogram;
        # categorical (string-labelled) columns get a categorical
        # bar-of-counts to keep the per-dimension chart layout
        # consistent without rendering a misleading numeric histogram
        # over count values.
        if source_col in df.columns and df[source_col].dtype.kind in ("i", "f"):
            values = pd.to_numeric(df[source_col], errors="coerce").to_numpy()
            fig = charts_mod.histogram(
                values,
                title=f"{pretty} — distribution",
                xlabel=pretty,
            )
        elif source_col in df.columns:
            counts = df[source_col].value_counts(dropna=False)
            fig = charts_mod.categorical_histogram(
                labels=[str(k) for k in counts.index.tolist()],
                counts=[int(v) for v in counts.values.tolist()],
                title=f"{pretty} — distribution",
                xlabel=pretty,
            )
        else:
            fig = charts_mod.histogram(
                np.array([], dtype=float),
                title=f"{pretty} — distribution",
                xlabel=pretty,
            )
        charts_mod.savefig_and_close(fig, charts_dir / f"{slug}__histogram.png")

        fig = charts_mod.bucket_mean_with_ci(
            buckets,
            title=f"{pretty} — mean return per bucket (95% bootstrap CI)",
            ylabel="Mean return",
        )
        charts_mod.savefig_and_close(fig, charts_dir / f"{slug}__bucket_mean_returns.png")

        fig = charts_mod.honesty_ratio_bars(
            buckets,
            title=f"{pretty} — honesty ratio per bucket",
        )
        charts_mod.savefig_and_close(fig, charts_dir / f"{slug}__honesty_ratio.png")

        fig = charts_mod.win_rate_bars(
            buckets,
            title=f"{pretty} — win rate per bucket",
        )
        charts_mod.savefig_and_close(fig, charts_dir / f"{slug}__win_rate.png")

    # Logit forest plot.  When the fit failed, render an empty plot
    # with a "fit failed" annotation rather than skipping — keeps the
    # report's image reference consistent.
    fig = charts_mod.logit_forest(
        logit.features or [],
        title=(
            "Logit coefficients (95% CI)"
            if logit.fitted
            else f"Logit not fitted: {logit.reason}"
        ),
    )
    charts_mod.savefig_and_close(fig, charts_dir / "logit__forest_plot.png")


# ── Markdown rendering ───────────────────────────────────────────────────────


def _render_markdown(
    summary: dict[str, Any],
    *,
    edge_buckets: list[BucketSummary],
    vol_buckets: list[BucketSummary],
    match_buckets: list[BucketSummary],
    staleness_buckets: list[BucketSummary],
    confidence_buckets: list[BucketSummary],
    logit: LogitResult,
    recommendations: list[Recommendation],
) -> str:
    """Assemble report.md from the summary dict + the bucket lists.

    Rendered as plain (GitHub-flavored) markdown.  Image references use
    relative paths (``charts/<file>.png``) so the bundle is portable.
    """
    parts: list[str] = []
    parts.append("# Paper-Ledger Calibration Report\n")
    parts.append(_render_header_table(summary))
    parts.append(_render_tier_banner(summary))
    parts.append(_render_schema_skips(summary))
    parts.append(_render_top_line(summary))
    parts.append("\n## Univariate Analyses\n")
    parts.append(_render_dimension_section(
        "Conservative Edge", "conservative_edge", edge_buckets, summary,
    ))
    parts.append(_render_dimension_section(
        "Vol Regime", "vol_regime_clean", vol_buckets, summary,
    ))
    parts.append(_render_dimension_section(
        "Match Quality", "match_quality", match_buckets, summary,
        boundaries_blurb=True,
    ))
    parts.append(_render_dimension_section(
        "Max Feed Staleness (ms)", "max_feed_staleness_ms", staleness_buckets, summary,
        boundaries_blurb=True,
    ))
    parts.append(_render_dimension_section(
        "Confidence", "confidence", confidence_buckets, summary,
        boundaries_blurb=True,
    ))
    parts.append(_render_crosstab(summary))
    parts.append(_render_logit_section(logit))
    parts.append(_render_open_positions(summary))
    parts.append(_render_recommendations(recommendations))
    return "\n".join(parts)


def _render_header_table(summary: dict[str, Any]) -> str:
    w = summary["dataset_window"]
    return (
        "| Field        | Value |\n"
        "|--------------|-------|\n"
        f"| Generated at | {w['generated_at']} |\n"
        f"| Ledger dir   | {summary.get('ledger_dir') or 'n/a'} |\n"
        f"| As-of filter | {w['as_of'] or 'n/a'} |\n"
        f"| Earliest order | {w['earliest_order_at'] or 'n/a'} |\n"
        f"| Latest order   | {w['latest_order_at'] or 'n/a'} |\n"
    )


def _render_tier_banner(summary: dict[str, Any]) -> str:
    tier = summary["tier_reached"]
    n_settled = summary["counts"]["n_settled"]
    flags = {
        "below_single_threshold": (
            "Below single-threshold tier — all analyses are indicative only."
        ),
        "single_threshold": (
            "Univariate analyses are calibration-quality (N≥300). "
            "Regime-conditional + multi-feature gated."
        ),
        "regime_conditional": (
            "Regime-conditional crosstabs unlocked (N≥800). "
            "Multi-feature logit summary still indicative."
        ),
        "multi_feature": (
            "All tiers unlocked (N≥1500). Multi-feature logit is "
            "calibration-quality."
        ),
    }
    return (
        f"\n## Power Tier: {tier.upper()} (N_settled = {n_settled})\n\n"
        f"> {flags[tier]}\n"
    )


def _render_schema_skips(summary: dict[str, Any]) -> str:
    skips = summary.get("schema_skips") or {}
    if not skips:
        return "\n## Schema-Skip Audit\n\nNo schema-skip data reported.\n"
    rows = ["| File | Loaded | Schema-skipped | Invalid-skipped |",
            "|------|-------:|---------------:|----------------:|"]
    for name in ("orders", "fills", "settlements"):
        d = skips.get(name, {})
        loaded = d.get("loaded", "n/a")
        unknown = d.get("unknown_schema_version", 0)
        invalid = d.get("invalid", 0)
        rows.append(f"| {name} | {loaded} | {unknown} | {invalid} |")
    note = ""
    nonzero = any(
        (d.get("unknown_schema_version", 0) or d.get("invalid", 0))
        for d in skips.values() if isinstance(d, dict)
    )
    if nonzero:
        note = (
            "\n> NOTE: non-zero skip counts. "
            "Investigate via `paper_ledger.parse_error` / "
            "`analyze.parse_error` / `analyze.unknown_schema_version` "
            "WARNING log lines.\n"
        )
    return "\n## Schema-Skip Audit\n\n" + "\n".join(rows) + "\n" + note


def _render_top_line(summary: dict[str, Any]) -> str:
    t = summary["top_line_stats"]
    c = summary["counts"]

    def fmt(v: float | None, places: int = 4) -> str:
        if v is None:
            return "n/a"
        return f"{v:.{places}f}"

    return (
        "\n## Top-Line Stats\n\n"
        "| Metric | Value |\n"
        "|--------|------:|\n"
        f"| n_orders | {c['n_orders']} |\n"
        f"| n_settled | {c['n_settled']} |\n"
        f"| n_open | {c['n_open']} |\n"
        f"| n_wins | {c['n_wins']} |\n"
        f"| n_losses | {c['n_losses']} |\n"
        f"| n_pushes | {c['n_pushes']} |\n"
        f"| overall_win_rate | {fmt(t['overall_win_rate'], 3)} |\n"
        f"| mean_realized_pnl | {fmt(t['mean_realized_pnl'], 2)} |\n"
        f"| mean_return | {fmt(t['mean_return'], 4)} |\n"
        f"| overall_honesty_ratio | {fmt(t['overall_honesty_ratio'], 2)} |\n"
    )


def _render_dimension_section(
    pretty_name: str,
    slug: str,
    buckets: list[BucketSummary],
    summary: dict[str, Any],
    *,
    boundaries_blurb: bool = False,
) -> str:
    quality_line = (
        "*Calibration-quality (N_settled ≥ 300 in at least one bucket).*"
        if any(b.quality_flag == "calibration" for b in buckets)
        else "*Indicative only — no bucket has reached N_settled ≥ 300.*"
    )
    bound_line = ""
    if boundaries_blurb:
        bm = summary["bucket_summaries"].get(slug, {})
        bounds = bm.get("boundaries") or []
        if bounds:
            bound_line = f"\n*Quartile boundaries: {bounds}.*\n"

    img_block = (
        f"\n![histogram](charts/{slug}__histogram.png)\n"
        f"![bucket means with bootstrap CI](charts/{slug}__bucket_mean_returns.png)\n"
        f"![honesty ratio per bucket](charts/{slug}__honesty_ratio.png)\n"
        f"![win rate per bucket](charts/{slug}__win_rate.png)\n"
    )

    table = _render_bucket_table(buckets)
    return f"\n### {pretty_name}\n{quality_line}\n{bound_line}{img_block}\n{table}\n"


def _render_bucket_table(buckets: list[BucketSummary]) -> str:
    if not buckets:
        return "(no buckets)\n"
    header = (
        "| Bucket | N | N_settled | N_eligible | Win Rate | Mean Return | "
        "95% CI | Honesty | Quality |\n"
        "|--------|--:|----------:|-----------:|---------:|------------:|"
        "--------|--------:|---------|"
    )
    lines = [header]
    for b in buckets:
        ci = (
            f"[{b.ci_low:.4f}, {b.ci_high:.4f}] ({b.ci_method})"
            if b.ci_low is not None and b.ci_high is not None
            else f"({b.ci_method})"
        )
        wr = f"{b.win_rate:.3f}" if b.win_rate is not None else "n/a"
        mr = f"{b.mean_return:.4f}" if b.mean_return is not None else "n/a"
        h = f"{b.honesty_ratio:.2f}" if b.honesty_ratio is not None else "n/a"
        lines.append(
            f"| {b.bucket} | {b.n} | {b.n_settled} | {b.n_eligible} | "
            f"{wr} | {mr} | {ci} | {h} | {b.quality_flag} |"
        )
    return "\n".join(lines) + "\n"


def _render_crosstab(summary: dict[str, Any]) -> str:
    ct = summary["regime_x_edge_crosstab"]
    if ct.get("gated"):
        n_have = ct.get("n_settled", "?")
        n_need = ct.get("n_required", TIER_REGIME_CONDITIONAL)
        return (
            "\n## Regime × Edge Crosstab\n\n"
            f"**Gated:** N_settled = {n_have} < {n_need} (regime_conditional).\n"
        )
    rows = ct.get("rows") or []
    if not rows:
        return "\n## Regime × Edge Crosstab\n\n(no eligible rows)\n"
    out = [
        "\n## Regime × Edge Crosstab\n",
        "| Vol regime | Edge bucket | N | Win Rate | Mean Return |",
        "|------------|-------------|--:|---------:|------------:|",
    ]
    for r in rows:
        out.append(
            f"| {r['vol_regime_clean']} | {r['conservative_edge_bucket']} | "
            f"{r['n']} | {r['win_rate']:.3f} | {r['mean_return']:.4f} |"
        )
    return "\n".join(out) + "\n"


def _render_logit_section(logit: LogitResult) -> str:
    parts = ["\n## Logistic Regression\n"]
    if not logit.fitted:
        parts.append(
            f"**Not fitted:** reason=`{logit.reason}`, n={logit.n}.\n"
        )
        parts.append("\n![logit forest plot](charts/logit__forest_plot.png)\n")
        return "".join(parts)

    qual = (
        "*Calibration-quality (N ≥ TIER_MULTI_FEATURE = 1500).*"
        if logit.quality_flag == "calibration"
        else f"*Indicative only — N = {logit.n} < TIER_MULTI_FEATURE (1500).*"
    )
    parts.append(qual + "\n\n")
    parts.append(
        "| Feature | Coef | Std Err | z | P>|z| | 95% CI |\n"
        "|---------|-----:|--------:|--:|-------|--------|\n"
    )
    for f in logit.features or []:
        parts.append(
            f"| {f.name} | {f.coef:.4f} | {f.std_err:.4f} | {f.z:.3f} | "
            f"{f.p_value:.4f} | [{f.ci_low:.4f}, {f.ci_high:.4f}] |\n"
        )
    parts.append(
        f"\nMcFadden's R²: {logit.mcfaddens_r2:.4f}\n"
        f"Convergence: {logit.converged}\n"
        f"Reference vol_regime: {logit.reference_vol_regime}\n"
    )
    cm = logit.confusion_matrix_at_05 or {}
    parts.append(
        "\nConfusion Matrix at cutoff 0.5:\n\n"
        "|             | Pred Loss | Pred Win |\n"
        "|-------------|----------:|---------:|\n"
        f"| Actual Loss | {cm.get('tn', 0)} | {cm.get('fp', 0)} |\n"
        f"| Actual Win  | {cm.get('fn', 0)} | {cm.get('tp', 0)} |\n"
    )
    parts.append("\n![logit forest plot](charts/logit__forest_plot.png)\n")
    return "".join(parts)


def _render_open_positions(summary: dict[str, Any]) -> str:
    op = summary["open_positions"]
    me = op["mean_conservative_edge"]
    me_str = f"{me:.4f}" if me is not None else "n/a"
    bp = ", ".join(f"{k}={v}" for k, v in (op.get("by_platform") or {}).items()) or "n/a"
    bs = ", ".join(f"{k}={v}" for k, v in (op.get("by_side") or {}).items()) or "n/a"
    return (
        "\n## Open Positions Snapshot\n\n"
        f"- N_open: {op['n_open']}\n"
        f"- Mean conservative_edge of open: {me_str}\n"
        f"- By platform: {bp}\n"
        f"- By side: {bs}\n"
    )


def _render_recommendations(recommendations: list[Recommendation]) -> str:
    parts = [
        "\n## Recommendations\n",
        "*Generated by 9b2's single-rule heuristic — see "
        "`tools.analysis.stats.generate_recommendations`. Multi-rule "
        "logic is 9c territory.*\n",
    ]
    if not recommendations:
        parts.append("\n(no recommendations triggered)\n")
        return "".join(parts)
    for r in recommendations:
        parts.append(
            f"\n- **{r.kind}** — {r.suggested_action}. "
            f"Evidence: {r.evidence}. *(quality_flag: {r.quality_flag})*\n"
        )
    return "".join(parts)
