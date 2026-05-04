"""Matplotlib chart factories for the calibration report.

Every factory returns a ``Figure`` so the orchestrator owns the
``savefig`` + ``plt.close`` lifecycle — that ordering is important
because matplotlib leaks memory if figures are not explicitly closed
in a generation loop, and we generate ~21 figures per report.

Filename convention (set by callers, but documented here so the
report-side image references stay grep-able):

    charts/<dimension>__<chart_kind>.png

Double underscore separator between dim and kind so a single grep finds
the same chart kind across dimensions:
``rg "honesty_ratio.png"`` returns one path per dimension.

Five dimensions × four chart kinds = 20 PNGs, plus one logit forest
plot for a total of 21 files at 150 DPI.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

# Use the non-interactive Agg backend so the script doesn't try to open
# a GUI window when run on a headless box.  Set before pyplot import.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (intentional post-use())
import numpy as np  # noqa: E402

if TYPE_CHECKING:
    from matplotlib.figure import Figure

    from tools.analysis.stats import BucketSummary, FeatureCoef


# Default DPI for all charts.  Matches the deliverable spec.
_DPI: int = 150


# ── Helpers ──────────────────────────────────────────────────────────────────


def savefig_and_close(fig: "Figure", path: Path) -> None:
    """Save ``fig`` to ``path`` at the report-standard 150 DPI, then close.

    The close call is load-bearing — matplotlib accumulates state per
    Figure and leaks memory if figures are not explicitly closed in a
    generation loop.  Centralizing here so callers can't forget.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)


def _annotate_empty_buckets(
    ax: "plt.Axes", buckets: list["BucketSummary"], y: float = 0.0,
) -> None:
    """Annotate empty / degenerate bucket slots with a small text label.

    Keeps the chart x-axis stable across runs (each bucket has a slot
    even when the data is empty) while making it visible to the reader
    that the slot has no data behind it.
    """
    for i, b in enumerate(buckets):
        if b.n_eligible == 0:
            ax.text(
                i, y, "n=0",
                ha="center", va="bottom",
                fontsize=7, color="gray", rotation=90,
            )
        elif b.n_eligible == 1:
            ax.text(
                i, y, "n=1 (degenerate)",
                ha="center", va="bottom",
                fontsize=7, color="gray", rotation=90,
            )


# ── Factories ────────────────────────────────────────────────────────────────


def histogram(
    values: np.ndarray, *, title: str, xlabel: str, bins: int | str = "auto",
) -> "Figure":
    """Univariate histogram of ``values`` (NaN-stripped).

    ``bins="auto"`` lets matplotlib pick a sensible bin count via the
    Freedman-Diaconis rule.  Empty input still produces a figure (with
    a "no data" annotation) so the report's image reference doesn't
    404.
    """
    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    fig, ax = plt.subplots(figsize=(7, 4))
    if len(finite) == 0:
        ax.text(
            0.5, 0.5, "no data",
            ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="gray",
        )
    else:
        ax.hist(finite, bins=bins, edgecolor="black", linewidth=0.5)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Count")
    fig.tight_layout()
    return fig


def categorical_histogram(
    labels: list[str], counts: list[int], *, title: str, xlabel: str,
) -> "Figure":
    """Bar chart of categorical-value counts.

    Used by the report when the source column is a string label
    (e.g. ``vol_regime_clean``) where a true numeric histogram would
    be misleading.  Fills the same slot in the report as
    :func:`histogram` so the per-dimension layout stays consistent.
    """
    fig, ax = plt.subplots(figsize=(7, 4))
    if not labels:
        ax.text(
            0.5, 0.5, "no data",
            ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="gray",
        )
    else:
        x_positions = np.arange(len(labels))
        ax.bar(x_positions, counts, edgecolor="black", linewidth=0.5)
        ax.set_xticks(x_positions)
        ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Count")
    fig.tight_layout()
    return fig


def bucket_mean_with_ci(
    buckets: list["BucketSummary"], *, title: str, ylabel: str,
) -> "Figure":
    """Bar chart of per-bucket mean return with bootstrap CI error bars.

    Empty / degenerate buckets occupy their x-slot but render no bar;
    they get a "n=0" or "n=1 (degenerate)" annotation instead.  This
    keeps the x-axis ordering consistent with the win-rate / honesty
    charts so the four charts of one dimension visually align.
    """
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if not buckets:
        ax.text(
            0.5, 0.5, "no buckets",
            ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="gray",
        )
        ax.set_title(title)
        fig.tight_layout()
        return fig

    x_positions = np.arange(len(buckets))
    means = np.array(
        [b.mean_return if b.mean_return is not None else np.nan for b in buckets],
        dtype=float,
    )
    lows = np.array(
        [b.ci_low if b.ci_low is not None else np.nan for b in buckets],
        dtype=float,
    )
    highs = np.array(
        [b.ci_high if b.ci_high is not None else np.nan for b in buckets],
        dtype=float,
    )
    # yerr expects [lower_offset, upper_offset] from the bar height.
    lower_err = means - lows
    upper_err = highs - means
    yerr = np.vstack([lower_err, upper_err])
    # NaN errors crash matplotlib's errorbar; replace with zero where
    # we have no CI (degenerate / empty buckets render no bar anyway).
    yerr = np.nan_to_num(yerr, nan=0.0)

    ax.bar(
        x_positions, means,
        yerr=yerr,
        capsize=4,
        edgecolor="black", linewidth=0.5,
    )
    ax.axhline(0, color="gray", linewidth=0.7)
    ax.set_xticks(x_positions)
    ax.set_xticklabels([b.bucket for b in buckets], rotation=30, ha="right")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    _annotate_empty_buckets(ax, buckets, y=0.0)
    fig.tight_layout()
    return fig


def honesty_ratio_bars(
    buckets: list["BucketSummary"], *, title: str,
) -> "Figure":
    """Bar chart of per-bucket honesty ratios with reference line at 1.0.

    A bar at 1.0 means realized return matched the forecast mean
    perfectly within the bucket; below 1.0 = optimistic forecast,
    above 1.0 = pessimistic forecast.  None values render as blank
    slots (kept for x-axis stability with the other three charts).
    """
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if not buckets:
        ax.text(
            0.5, 0.5, "no buckets",
            ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="gray",
        )
        ax.set_title(title)
        fig.tight_layout()
        return fig

    x_positions = np.arange(len(buckets))
    ratios = np.array(
        [b.honesty_ratio if b.honesty_ratio is not None else np.nan for b in buckets],
        dtype=float,
    )

    ax.bar(x_positions, ratios, edgecolor="black", linewidth=0.5)
    ax.axhline(1.0, color="green", linewidth=0.8, linestyle="--", label="perfect calibration")
    ax.set_xticks(x_positions)
    ax.set_xticklabels([b.bucket for b in buckets], rotation=30, ha="right")
    ax.set_title(title)
    ax.set_ylabel("Honesty ratio (realized / forecast)")
    ax.legend(loc="best", fontsize=8)
    _annotate_empty_buckets(ax, buckets, y=0.0)
    fig.tight_layout()
    return fig


def win_rate_bars(
    buckets: list["BucketSummary"], *, title: str,
) -> "Figure":
    """Bar chart of per-bucket win rate with reference line at 0.5.

    Win rate is computed over the eligible (non-push, settled) subset
    by :func:`bucket_summary`.  None values render as blank slots.
    """
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if not buckets:
        ax.text(
            0.5, 0.5, "no buckets",
            ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="gray",
        )
        ax.set_title(title)
        fig.tight_layout()
        return fig

    x_positions = np.arange(len(buckets))
    rates = np.array(
        [b.win_rate if b.win_rate is not None else np.nan for b in buckets],
        dtype=float,
    )

    ax.bar(x_positions, rates, edgecolor="black", linewidth=0.5)
    ax.axhline(0.5, color="gray", linewidth=0.8, linestyle="--", label="chance")
    ax.set_xticks(x_positions)
    ax.set_xticklabels([b.bucket for b in buckets], rotation=30, ha="right")
    ax.set_ylim(0, 1)
    ax.set_title(title)
    ax.set_ylabel("Win rate")
    ax.legend(loc="best", fontsize=8)
    _annotate_empty_buckets(ax, buckets, y=0.0)
    fig.tight_layout()
    return fig


def logit_forest(
    features: list["FeatureCoef"], *, title: str = "Logit coefficients (95% CI)",
    exclude_intercept: bool = True,
) -> "Figure":
    """Horizontal forest plot of logit coefficients with 95% CIs.

    Sorted by |coef| descending so the most-influential features sit at
    the top.  Intercept is excluded by default (its interpretation is
    "log-odds at all-zero features," rarely meaningful for our feature
    set).  Vertical reference line at coef=0.
    """
    rows = list(features)
    if exclude_intercept:
        rows = [f for f in rows if f.name not in ("const", "Intercept")]
    rows.sort(key=lambda f: abs(f.coef), reverse=True)

    fig, ax = plt.subplots(figsize=(8, 0.4 * max(len(rows), 1) + 1.5))
    if not rows:
        ax.text(
            0.5, 0.5, "no features",
            ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="gray",
        )
        ax.set_title(title)
        fig.tight_layout()
        return fig

    y_positions = np.arange(len(rows))[::-1]  # top = highest |coef|
    coefs = np.array([r.coef for r in rows])
    lows = np.array([r.ci_low for r in rows])
    highs = np.array([r.ci_high for r in rows])
    lower_err = coefs - lows
    upper_err = highs - coefs

    ax.errorbar(
        coefs, y_positions,
        xerr=np.vstack([lower_err, upper_err]),
        fmt="o", color="black", ecolor="gray", capsize=3,
    )
    ax.axvline(0, color="red", linewidth=0.8, linestyle="--")
    ax.set_yticks(y_positions)
    ax.set_yticklabels([r.name for r in rows])
    ax.set_xlabel("Coefficient (log-odds)")
    ax.set_title(title)
    fig.tight_layout()
    return fig
