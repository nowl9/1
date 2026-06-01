"""Statistical primitives for paper-ledger calibration analysis.

Pure functions over pandas DataFrames and numpy arrays — no I/O, no
plotting.  The contract surface is small on purpose so the library-direct
tests in ``test_analyze_paper_ledger_9b2.py`` can exercise each piece
without touching the orchestrator.

Design choices worth flagging
-----------------------------
* **Logit, not linear regression.**  Realized P&L on Kalshi binaries is
  bimodal (entry-price-dependent payoffs); a linear model on raw
  ``realized_pnl`` would be dominated by entry-price variance and
  systematically mis-attribute coefficient mass.  This was litigated in
  earlier rounds and rejected — the comment is here so a future
  contributor doesn't quietly reintroduce it.

* **Bootstrap CIs use BCa with a percentile fallback.**  BCa needs a
  jackknife-variance computation that fails on degenerate inputs
  (constant arrays, N=2 with equal values).  We catch the failure and
  fall back to the percentile method, recording which method was used
  in the returned ``ci_method`` string so the report can flag CIs that
  came from the fallback.

* **Quartile boundaries computed on the full dataset, not the
  settled-only subset.**  Bucket boundaries that drift when settlement
  rates change make cross-run comparisons unreliable; computing on the
  full dataset stabilizes them.  Flagged in ``BucketSummary.bucket_kind``
  and the boundaries themselves are written to ``summary_stats.json``
  so a reader can verify against expectations.

* **Honesty ratio is per-bucket, not just overall.**  Within-bucket
  bias is the load-bearing diagnostic for whether ``conservative_edge``
  (= ``adjusted_edge`` on disk) is a calibrated estimator at each
  level — if the 1.0-1.5% bucket has honesty 0.5, the calibration is
  systematically optimistic at the floor regardless of what the overall
  ratio says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.stats import bootstrap

# statsmodels is an optional ``[analysis]`` extra; importing here so a
# missing install fails loudly with the dependency-suggestion error
# rather than at fit_logit() call time.  The runtime agent does not
# import this module, so the dep stays out of the agent's process.
import statsmodels.api as sm
from statsmodels.tools.sm_exceptions import PerfectSeparationError


# ── Tier constants (re-exported for downstream gating) ───────────────────────

# Mirrors tools/analyze_paper_ledger.py — duplicated here so callers of
# the analysis library don't have to depend on the orchestrator module.
# The values themselves come from the Round 9 power review.
TIER_SINGLE_THRESHOLD: int = 300
TIER_REGIME_CONDITIONAL: int = 800
TIER_MULTI_FEATURE: int = 1500


# Honesty-ratio threshold for the raise_edge_floor recommendation.  The
# 0.75 cutoff is the conservative single-rule trigger 9b2 ships; richer
# multi-rule heuristics are deferred to 9c.
HONESTY_RECOMMEND_THRESHOLD: float = 0.75


# ── Result dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class BucketSummary:
    """One row of a per-bucket univariate analysis.

    A list of these is the output of :func:`bucket_summary`.  Designed to
    serialize cleanly via :func:`dataclasses.asdict` into the
    ``summary_stats.json["bucket_summaries"][dim]["rows"]`` array.

    Field semantics worth flagging:

    * ``n_eligible`` — count of settled rows whose ``outcome`` is
      ``"win"`` or ``"loss"``.  Excludes ``"push"`` because the binary
      win-rate metric is undefined for pushes.
    * ``ci_method`` — one of ``"BCa"``, ``"percentile_fallback"``,
      ``"degenerate_n=1"``, ``"degenerate_zero_variance"``, ``"n=0"``.
      Lets the report distinguish a real CI from a degenerate-input
      placeholder.
    * ``honesty_ratio`` — ``mean(return) / mean(theoretical_edge)`` over
      the eligible (non-push, settled) subset.  ``None`` when the
      denominator is zero, undefined, or the bucket has no eligible
      rows; downstream readers must handle ``None`` (renders as
      ``"n/a"`` in the markdown table).
    * ``quality_flag`` — ``"calibration"`` when ``n_settled >=
      TIER_SINGLE_THRESHOLD``, else ``"indicative"``.  Drives
      report-side italics + recommendation gating.
    """

    bucket: str
    n: int
    n_settled: int
    n_eligible: int
    win_rate: float | None
    mean_return: float | None
    ci_low: float | None
    ci_high: float | None
    ci_method: str
    mean_theoretical_edge: float | None
    honesty_ratio: float | None
    quality_flag: Literal["calibration", "indicative"]


@dataclass(frozen=True)
class FeatureCoef:
    """One row of a logit coefficient table."""

    name: str
    coef: float
    std_err: float
    z: float
    p_value: float
    ci_low: float
    ci_high: float


@dataclass(frozen=True)
class LogitResult:
    """Outcome of :func:`fit_logit` — fitted-or-skipped union.

    When ``fitted`` is False the other fields except ``reason`` and ``n``
    are ``None``; when True they are populated.  Surfaced via
    ``summary_stats.json["logit_coefficients"]``.

    ``quality_flag`` is a structured field (not just a report label) so
    9c's reader can dispatch programmatically: ``"calibration"`` only
    when ``n >= TIER_MULTI_FEATURE``, else ``"indicative"``.
    """

    fitted: bool
    reason: str | None = None
    n: int = 0
    features: list[FeatureCoef] | None = None
    mcfaddens_r2: float | None = None
    converged: bool | None = None
    confusion_matrix_at_05: dict[str, int] | None = None
    reference_vol_regime: str | None = None
    quality_flag: Literal["calibration", "indicative"] | None = None


@dataclass(frozen=True)
class Recommendation:
    """One row of the recommendations array.

    9b2 emits exactly one heuristic — ``raise_edge_floor`` when a
    calibration-quality conservative_edge bucket has honesty < 0.75.
    Multi-rule logic is 9c territory.
    """

    kind: str
    evidence: str
    suggested_action: str
    quality_flag: Literal["calibration", "indicative"]


# ── Bootstrap CI ─────────────────────────────────────────────────────────────


def bootstrap_mean_ci(
    values: np.ndarray | pd.Series,
    *,
    n_resamples: int = 1000,
    confidence_level: float = 0.95,
    seed: int = 42,
) -> tuple[float | None, float | None, str]:
    """Bootstrap CI for the mean of ``values``.

    Returns ``(ci_low, ci_high, ci_method)``.  Both bounds are ``None``
    only when the input is empty.  For degenerate inputs (N=1 or zero
    variance) the bounds equal the point estimate and ``ci_method``
    encodes which degenerate case fired.

    Method strategy:
    * BCa is the primary method (unbiased + accelerated → tighter, more
      accurate CIs on skewed data, which P&L typically is).
    * If BCa raises (typically ``ValueError`` when the jackknife
      variance is zero), fall back to ``"percentile"`` and record the
      fallback in ``ci_method``.
    * The seed is pinned at 42 so re-running the report on the same data
      produces byte-identical CIs — important for the "did the
      recommendation change?" diff between runs.
    """
    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    n = len(finite)
    if n == 0:
        return (None, None, "n=0")
    if n == 1:
        v = float(finite[0])
        return (v, v, "degenerate_n=1")
    if finite.min() == finite.max():
        # Exact-equality check on min/max rather than ``np.std == 0``:
        # for arithmetically-identical inputs ``[v, v, v]``, np.std
        # computes ``mean = 3v/3`` which doesn't recover v exactly in
        # float64 (1-ULP residual), leaving a ~1e-17 std that the
        # ``== 0.0`` guard misses.  min == max is exact because
        # subtraction of identical floats is exactly zero.
        v = float(finite[0])
        return (v, v, "degenerate_zero_variance")
    rng = np.random.default_rng(seed)
    try:
        res = bootstrap(
            (finite,),
            np.mean,
            n_resamples=n_resamples,
            confidence_level=confidence_level,
            method="BCa",
            random_state=rng,
        )
        return (
            float(res.confidence_interval.low),
            float(res.confidence_interval.high),
            "BCa",
        )
    except (ValueError, ZeroDivisionError):
        # BCa needs a non-degenerate jackknife; fall back to percentile
        # which has no such requirement.  Re-seed to keep determinism
        # across the fallback path.
        res = bootstrap(
            (finite,),
            np.mean,
            n_resamples=n_resamples,
            confidence_level=confidence_level,
            method="percentile",
            random_state=np.random.default_rng(seed),
        )
        return (
            float(res.confidence_interval.low),
            float(res.confidence_interval.high),
            "percentile_fallback",
        )


# ── Honesty ratio ────────────────────────────────────────────────────────────


def honesty_ratio(
    returns: np.ndarray | pd.Series,
    theoretical_edges: np.ndarray | pd.Series,
) -> float | None:
    """Compute ``mean(returns) / mean(theoretical_edges)``.

    Returns ``None`` rather than ±inf when:

    * Either array is empty after dropping NaN.
    * ``mean(theoretical_edges) == 0`` (undefined).
    * Either array is entirely NaN.

    Caller is responsible for filtering to the eligible subset before
    calling — this function is a pure ratio and does not know about
    settlement state, push outcomes, etc.
    """
    r = np.asarray(returns, dtype=float)
    te = np.asarray(theoretical_edges, dtype=float)
    r_finite = r[np.isfinite(r)]
    te_finite = te[np.isfinite(te)]
    if len(r_finite) == 0 or len(te_finite) == 0:
        return None
    mean_te = float(np.mean(te_finite))
    if mean_te == 0.0:
        return None
    return float(np.mean(r_finite)) / mean_te


# ── Bucket summary ───────────────────────────────────────────────────────────


def _quality_flag_for(n_settled: int) -> Literal["calibration", "indicative"]:
    """Map a per-bucket ``n_settled`` to the quality-flag enum.

    The ``TIER_SINGLE_THRESHOLD`` cutoff is the per-bucket calibration
    threshold the Round 9 power review picked.  Below it, the bucket is
    "indicative only" — the report flags it and the recommendations
    generator gates on it.
    """
    return "calibration" if n_settled >= TIER_SINGLE_THRESHOLD else "indicative"


def bucket_summary(
    df: pd.DataFrame,
    bucket_col: str,
    bucket_order: list[str] | None = None,
) -> list[BucketSummary]:
    """Per-bucket stats over a categorical ``bucket_col`` in ``df``.

    Operates on the joined DataFrame from :func:`build_joined_dataframe`.
    Skips ``NaN`` bucket values silently.  When ``bucket_order`` is
    provided, output rows follow that order (and missing-from-data
    buckets are emitted as empty rows so the chart x-axis stays
    consistent across runs); otherwise output is in observed-data order.

    Within each bucket the function computes:

    * ``n``, ``n_settled``, ``n_eligible`` (settled + outcome ∈
      {"win","loss"})
    * ``win_rate`` over the eligible subset (None if eligible=0)
    * ``mean_return`` + ``bootstrap_mean_ci`` over the eligible subset
    * ``mean_theoretical_edge`` + ``honesty_ratio`` over the eligible
      subset
    * ``quality_flag`` from ``n_settled``
    """
    if df.empty or bucket_col not in df.columns:
        return []

    grouped = df.groupby(bucket_col, dropna=True, observed=True)

    rows: list[BucketSummary] = []
    seen_buckets: set[str] = set()

    for bucket_value, sub in grouped:
        bucket_label = str(bucket_value)
        seen_buckets.add(bucket_label)
        rows.append(_summarize_bucket(bucket_label, sub))

    if bucket_order is not None:
        # Pad missing buckets so the chart x-axis is stable across runs.
        for b in bucket_order:
            if b not in seen_buckets:
                rows.append(_empty_bucket_summary(b))
        # Reorder rows to match bucket_order.
        order_index = {b: i for i, b in enumerate(bucket_order)}
        rows.sort(key=lambda r: order_index.get(r.bucket, len(bucket_order)))
    return rows


def _summarize_bucket(bucket: str, sub: pd.DataFrame) -> BucketSummary:
    """Compute one BucketSummary row from a non-empty group sub-frame."""
    n = len(sub)
    settled_mask = sub.get("is_settled", pd.Series([False] * n, index=sub.index))
    settled_mask = settled_mask.fillna(False).astype(bool)
    settled = sub[settled_mask]
    n_settled = len(settled)

    if "outcome" in settled.columns:
        eligible_mask = settled["outcome"].isin(["win", "loss"])
        eligible = settled[eligible_mask]
    else:
        eligible = settled.iloc[0:0]
    n_eligible = len(eligible)

    if n_eligible == 0:
        return BucketSummary(
            bucket=bucket,
            n=n,
            n_settled=n_settled,
            n_eligible=0,
            win_rate=None,
            mean_return=None,
            ci_low=None,
            ci_high=None,
            ci_method="n=0",
            mean_theoretical_edge=None,
            honesty_ratio=None,
            quality_flag=_quality_flag_for(n_settled),
        )

    win_rate = float((eligible["outcome"] == "win").mean())
    returns = pd.to_numeric(eligible["return"], errors="coerce")
    mean_return = float(np.nanmean(returns)) if returns.notna().any() else None
    ci_low, ci_high, ci_method = bootstrap_mean_ci(returns)

    if "theoretical_edge" in eligible.columns:
        te = pd.to_numeric(eligible["theoretical_edge"], errors="coerce")
        mean_te = float(np.nanmean(te)) if te.notna().any() else None
    else:
        te = pd.Series(dtype=float)
        mean_te = None

    h_ratio = honesty_ratio(returns.to_numpy(), te.to_numpy())

    return BucketSummary(
        bucket=bucket,
        n=n,
        n_settled=n_settled,
        n_eligible=n_eligible,
        win_rate=win_rate,
        mean_return=mean_return,
        ci_low=ci_low,
        ci_high=ci_high,
        ci_method=ci_method,
        mean_theoretical_edge=mean_te,
        honesty_ratio=h_ratio,
        quality_flag=_quality_flag_for(n_settled),
    )


def _empty_bucket_summary(bucket: str) -> BucketSummary:
    """Placeholder row for an ordered bucket that has no observations.

    Keeps chart x-axis ordering consistent across runs by emitting a
    visible-but-empty row instead of silently dropping the slot.
    """
    return BucketSummary(
        bucket=bucket,
        n=0,
        n_settled=0,
        n_eligible=0,
        win_rate=None,
        mean_return=None,
        ci_low=None,
        ci_high=None,
        ci_method="n=0",
        mean_theoretical_edge=None,
        honesty_ratio=None,
        quality_flag="indicative",
    )


# ── Quartile assignment ──────────────────────────────────────────────────────


def assign_quartile_buckets(
    df: pd.DataFrame,
    col: str,
    *,
    n_quartiles: int = 4,
) -> tuple[pd.Series, list[float]]:
    """Assign quartile bucket labels to ``df[col]``, computed over the
    full (open + settled) dataset.

    Returns ``(labels_series, boundaries)`` where ``labels_series`` is a
    string Series ("q1".."q4", with NaN where ``df[col]`` is NaN), and
    ``boundaries`` is the bin-edge list (length ``n_quartiles + 1``).

    Computing quartile boundaries on the full dataset (rather than the
    settled-only subset) stabilizes them across runs that have
    different settlement rates.  The boundaries themselves are written
    to ``summary_stats.json`` so a reader can verify and a future run
    can detect drift.

    When ``df[col]`` has fewer than ``n_quartiles`` distinct values,
    ``pd.qcut(duplicates="drop")`` reduces the bucket count; this
    function copes with the resulting <4 unique labels by using
    whatever labels qcut emits.
    """
    if df.empty or col not in df.columns:
        return pd.Series(dtype=object), []
    series = pd.to_numeric(df[col], errors="coerce")
    finite = series.dropna()
    if finite.empty:
        return pd.Series([np.nan] * len(df), index=df.index, dtype=object), []
    try:
        labelled, bins = pd.qcut(
            finite,
            q=n_quartiles,
            labels=[f"q{i + 1}" for i in range(n_quartiles)],
            duplicates="drop",
            retbins=True,
        )
    except ValueError:
        # qcut raises if all values are identical AND duplicates="raise";
        # we set duplicates="drop", but very-degenerate data can still
        # produce a single-bin result that qcut may flag.  Return a
        # single-bucket assignment in that case.
        bucket_label = "q1"
        labels_full = pd.Series(
            [bucket_label if not np.isnan(v) else np.nan for v in series],
            index=df.index,
            dtype=object,
        )
        return labels_full, [float(finite.min()), float(finite.max())]

    # Re-index labelled back to df's full index, padding with NaN where
    # the source value was NaN.
    labels_full = pd.Series([np.nan] * len(df), index=df.index, dtype=object)
    labels_full.loc[finite.index] = labelled.astype(str).values
    return labels_full, [float(b) for b in bins]


# ── Logit ────────────────────────────────────────────────────────────────────


_LOGIT_MIN_N: int = 30
_LOGIT_FEATURE_COLS: list[str] = [
    "adjusted_edge",
    "confidence",
    "match_quality",
    "max_feed_staleness_ms",
    "strike_gap_pct",
    "expiry_gap_hours",
]


def fit_logit(df: pd.DataFrame) -> LogitResult:
    """Fit a binary logit ``P(outcome="win" | features)``.

    Eligible subset: ``is_settled & outcome ∈ {"win","loss"}``.  Push
    outcomes (rare) are dropped because the binary target is undefined.

    Features:

    * Numeric: ``adjusted_edge``, ``confidence``, ``match_quality``,
      ``max_feed_staleness_ms``, ``strike_gap_pct``, ``expiry_gap_hours``
    * Categorical: ``vol_regime_clean`` one-hot encoded with one
      reference level dropped.  The reference level is the
      alphabetically-first observed value (deterministic given the
      ``"high" / "low" / "normal"`` set → reference = ``"high"``).

    Returns a ``LogitResult`` with ``fitted=False`` plus a ``reason``
    string when the model can't be fit:

    * ``"insufficient_settled_rows"`` — n < 30 after eligibility filter
    * ``"single_class_outcome"`` — all wins or all losses
    * ``"insufficient_after_dropna"`` — feature NaN dropping leaves <30
    * ``"fit_failed: <ExceptionType>"`` — statsmodels raised during fit

    ``quality_flag`` on the result is ``"calibration"`` only when
    ``n >= TIER_MULTI_FEATURE``; below that it is ``"indicative"``.
    """
    if df.empty:
        return LogitResult(fitted=False, reason="empty_dataframe", n=0)

    settled = df[df["is_settled"].fillna(False).astype(bool)]
    # No settlements merged -> the join never produced an "outcome" column
    # (e.g. a fresh run that placed orders but has not settled any).  Treat it
    # the same as too-few-settled rather than raising KeyError 'outcome'.
    if "outcome" not in settled.columns:
        return LogitResult(fitted=False, reason="insufficient_settled_rows", n=0)
    eligible = settled[settled["outcome"].isin(["win", "loss"])].copy()
    n = len(eligible)
    if n < _LOGIT_MIN_N:
        return LogitResult(fitted=False, reason="insufficient_settled_rows", n=n)
    if eligible["outcome"].nunique() < 2:
        return LogitResult(fitted=False, reason="single_class_outcome", n=n)

    missing_cols = [c for c in _LOGIT_FEATURE_COLS if c not in eligible.columns]
    if missing_cols:
        return LogitResult(
            fitted=False,
            reason=f"missing_feature_columns: {missing_cols}",
            n=n,
        )

    X_num = eligible[_LOGIT_FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    X_regime = pd.get_dummies(
        eligible.get("vol_regime_clean", pd.Series([], dtype=object)),
        prefix="vol",
        drop_first=True,
        dtype=float,
    )
    # Determine reference category for reporting.  drop_first=True drops
    # the alphabetically-first dummy column; the dropped value is the
    # reference category.
    all_regimes = sorted(eligible.get("vol_regime_clean", pd.Series(dtype=object))
                         .dropna().unique().tolist())
    reference_vol_regime = all_regimes[0] if all_regimes else None

    X = pd.concat([X_num, X_regime], axis=1).dropna()
    y = (eligible.loc[X.index, "outcome"] == "win").astype(int)
    if len(X) < _LOGIT_MIN_N:
        return LogitResult(
            fitted=False,
            reason="insufficient_after_dropna",
            n=len(X),
        )
    if y.nunique() < 2:
        return LogitResult(
            fitted=False,
            reason="single_class_outcome_after_dropna",
            n=len(X),
        )

    X_with_const = sm.add_constant(X, has_constant="add")

    try:
        model = sm.Logit(y, X_with_const).fit(disp=0, maxiter=100)
    except (PerfectSeparationError, np.linalg.LinAlgError, ValueError) as exc:
        return LogitResult(
            fitted=False,
            reason=f"fit_failed: {type(exc).__name__}",
            n=len(X),
        )

    features = _coefs_from_model(model)
    cm = _confusion_matrix_from_model(model, X_with_const, y)

    # McFadden's R²: 1 - (LL_full / LL_null).  Guard against LL_null
    # being zero (degenerate) by returning 0.0.
    if model.llnull == 0:
        r2 = 0.0
    else:
        r2 = float(1.0 - (model.llf / model.llnull))

    n_obs = int(len(X_with_const))
    quality = "calibration" if n_obs >= TIER_MULTI_FEATURE else "indicative"

    return LogitResult(
        fitted=True,
        n=n_obs,
        features=features,
        mcfaddens_r2=r2,
        converged=bool(model.mle_retvals.get("converged", False)),
        confusion_matrix_at_05=cm,
        reference_vol_regime=reference_vol_regime,
        quality_flag=quality,
    )


def _coefs_from_model(model: Any) -> list[FeatureCoef]:
    """Extract the coefficient table from a fitted statsmodels Logit.

    Uses ``model.conf_int()`` (default 95% CI).  Coefficient ordering
    matches the design matrix ordering.
    """
    params = model.params
    bse = model.bse
    z = model.tvalues
    p = model.pvalues
    ci = model.conf_int()
    rows: list[FeatureCoef] = []
    for name in params.index:
        rows.append(
            FeatureCoef(
                name=str(name),
                coef=float(params[name]),
                std_err=float(bse[name]),
                z=float(z[name]),
                p_value=float(p[name]),
                ci_low=float(ci.loc[name, 0]),
                ci_high=float(ci.loc[name, 1]),
            )
        )
    return rows


def _confusion_matrix_from_model(
    model: Any, X: pd.DataFrame, y: pd.Series, cutoff: float = 0.5,
) -> dict[str, int]:
    """2×2 confusion matrix at the given probability cutoff.

    Returns ``{tn, fp, fn, tp}`` as ints.  Used by both the markdown
    report and ``summary_stats.json``.
    """
    pred = (model.predict(X) >= cutoff).astype(int)
    tp = int(((y == 1) & (pred == 1)).sum())
    tn = int(((y == 0) & (pred == 0)).sum())
    fp = int(((y == 0) & (pred == 1)).sum())
    fn = int(((y == 1) & (pred == 0)).sum())
    return {"tn": tn, "fp": fp, "fn": fn, "tp": tp}


# ── Open-positions snapshot ──────────────────────────────────────────────────


def open_positions_snapshot(df: pd.DataFrame) -> dict[str, Any]:
    """Compute the open-positions section of the report.

    Counts and per-platform / per-side splits over the unsettled subset
    of the joined DataFrame.  Returned dict matches the
    ``summary_stats.json["open_positions"]`` shape.
    """
    if df.empty:
        return {
            "n_open": 0,
            "mean_conservative_edge": None,
            "by_platform": {},
            "by_side": {},
        }
    open_df = df[~df["is_settled"].fillna(False).astype(bool)]
    if open_df.empty:
        return {
            "n_open": 0,
            "mean_conservative_edge": None,
            "by_platform": {},
            "by_side": {},
        }
    edge = pd.to_numeric(open_df["adjusted_edge"], errors="coerce")
    mean_edge = float(np.nanmean(edge)) if edge.notna().any() else None
    by_platform = (
        open_df["platform"].value_counts(dropna=False).to_dict()
        if "platform" in open_df.columns
        else {}
    )
    by_side = (
        open_df["side"].value_counts(dropna=False).to_dict()
        if "side" in open_df.columns
        else {}
    )
    return {
        "n_open": int(len(open_df)),
        "mean_conservative_edge": mean_edge,
        "by_platform": {str(k): int(v) for k, v in by_platform.items()},
        "by_side": {str(k): int(v) for k, v in by_side.items()},
    }


# ── Recommendations ──────────────────────────────────────────────────────────


def generate_recommendations(
    edge_buckets: list[BucketSummary],
) -> list[Recommendation]:
    """9b2's single-rule recommendation engine.

    Rule: for each conservative-edge bucket where ``quality_flag ==
    "calibration"`` and ``honesty_ratio < HONESTY_RECOMMEND_THRESHOLD``
    (0.75), emit a ``raise_edge_floor`` recommendation pointing at the
    bucket's upper boundary.  Multi-rule logic is 9c territory; 9b2
    stays conservative on purpose.

    The "calibration" gate is load-bearing: without it, low-N
    indicative buckets would generate noise.  An indicative bucket with
    honesty 0.5 means almost nothing statistically; the gate ensures
    we only act on signals that have crossed the per-bucket calibration
    threshold.

    The recommendation's ``quality_flag`` echoes the bucket's flag —
    always ``"calibration"`` here because that's the gate, but the
    field is kept for forward-compatibility with future heuristics
    that may emit indicative recommendations.
    """
    recs: list[Recommendation] = []
    for b in edge_buckets:
        if b.quality_flag != "calibration":
            continue
        if b.honesty_ratio is None:
            continue
        if b.honesty_ratio >= HONESTY_RECOMMEND_THRESHOLD:
            continue
        # Bucket label is e.g. "1.0-1.5%" or "below_min" or "10%+";
        # extract the upper boundary for the suggestion.  Keep the
        # original bucket label in the evidence string so a reader can
        # cross-reference.
        upper = _bucket_upper_boundary_label(b.bucket)
        recs.append(
            Recommendation(
                kind="raise_edge_floor",
                evidence=(
                    f"bucket={b.bucket} honesty={b.honesty_ratio:.2f} "
                    f"(< {HONESTY_RECOMMEND_THRESHOLD:.2f}), "
                    f"n_eligible={b.n_eligible}"
                ),
                suggested_action=f"raise floor to {upper}",
                quality_flag="calibration",
            )
        )
    return recs


def _bucket_upper_boundary_label(bucket_label: str) -> str:
    """Extract a human-readable upper-boundary string from a bucket label.

    Handles the conservative-edge bucket label shapes:
    ``"1.0-1.5%"`` → ``"1.5%"``; ``"10%+"`` → ``"above 10%"``;
    ``"below_min"`` → ``"1.0%"`` (the floor); anything else → returned
    verbatim.
    """
    if bucket_label == "below_min":
        return "1.0%"
    if bucket_label.endswith("+"):
        return f"above {bucket_label[:-1]}"
    if "-" in bucket_label:
        # e.g. "1.0-1.5%" → "1.5%"
        upper = bucket_label.split("-", 1)[1]
        return upper
    return bucket_label
