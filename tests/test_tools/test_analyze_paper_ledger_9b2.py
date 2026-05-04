"""Library-direct tests for the 9b2 analysis package.

9b2 splits into 9b2a (library + library-direct tests) and 9b2b
(orchestrator wiring + new orchestrator-level tests).  This file is
the 9b2a half — every test here exercises a function in
``tools/analysis/{stats,charts,report}.py`` against an in-memory
DataFrame produced by ``build_joined_dataframe``.  No test in this
file invokes ``main()`` or writes JSONL files; that coverage is added
in 9b2b's separate test file so the two commits stay purely additive
on tests.

Fixture helpers ``_make_order`` / ``_make_fill`` / ``_make_settlement``
are imported from the 9b1 test module — same builders, no duplication.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperOrderRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.models import DataSource
from tests.test_tools.test_analyze_paper_ledger import (
    _NOW,
    _make_fill,
    _make_order,
    _make_settlement,
)
from tools.analysis import charts as charts_mod
from tools.analysis import report as report_mod
from tools.analysis.stats import (
    BucketSummary,
    HONESTY_RECOMMEND_THRESHOLD,
    TIER_REGIME_CONDITIONAL,
    TIER_SINGLE_THRESHOLD,
    bootstrap_mean_ci,
    bucket_summary,
    fit_logit,
    generate_recommendations,
    honesty_ratio,
)
from tools.analyze_paper_ledger import build_joined_dataframe


# ── Synthetic-ledger helpers ─────────────────────────────────────────────────


def _synthesize_settled_ledger(
    n: int,
    *,
    win_rate: float = 0.5,
    seed: int = 42,
) -> tuple[
    list[PaperOrderRecord], list[PaperFillRecord], list[PaperSettlementRecord]
]:
    """Generate ``n`` settled orders with deterministic feature variation.

    Used by tests that need realistic-shaped inputs to the bucket and
    logit machinery without exercising the full ledger I/O path.

    Feature mix:
    * adjusted_edge: uniform(0.005, 0.10) — spans all conservative-edge
      bins including ``below_min``.
    * confidence: uniform(0.4, 0.95)
    * match_quality: uniform(0.5, 1.0)
    * vol_regime: cycled through low/normal/high
    * feed_staleness_ms: uniform(50, 800) per feed
    * Outcome: ``"win"`` with probability ``win_rate``, else ``"loss"``.
      Realized P&L is +size_usd on win, -size_usd*entry_price on loss
      (binary contract payoff shape).
    """
    rng = np.random.default_rng(seed)
    orders: list[PaperOrderRecord] = []
    fills: list[PaperFillRecord] = []
    settlements: list[PaperSettlementRecord] = []
    regimes = ["VolRegime.LOW", "VolRegime.NORMAL", "VolRegime.HIGH"]
    for i in range(n):
        oid = f"syn-{i:04d}"
        edge = float(rng.uniform(0.005, 0.10))
        conf = float(rng.uniform(0.4, 0.95))
        mq = float(rng.uniform(0.5, 1.0))
        regime = regimes[i % 3]
        feed_st = {
            "deribit": float(rng.uniform(50, 800)),
            "kalshi": float(rng.uniform(50, 800)),
            "polymarket": float(rng.uniform(50, 800)),
        }
        order = _make_order(
            client_order_id=oid,
            adjusted_edge=edge,
            confidence=conf,
            match_quality=mq,
            vol_regime=regime,
            feed_staleness_ms=feed_st,
        )
        # Mutate the strike_gap_pct / expiry_gap_hours via _make_order
        # would require a kwarg the helper doesn't expose; instead,
        # rebuild the record with explicit values.  The 9b1 helper
        # defaults both to 0.0, which would give the logit a constant
        # column and a singular design matrix — vary them explicitly
        # here so fit_logit has signal to fit on.
        order = order.model_copy(update={
            "strike_gap_pct": float(rng.uniform(0.0, 0.05)),
            "expiry_gap_hours": float(rng.uniform(0.0, 6.0)),
        })
        orders.append(order)
        fills.append(_make_fill(client_order_id=oid))
        is_win = rng.random() < win_rate
        outcome = "win" if is_win else "loss"
        # Binary payoff: win → +(1-entry)*size, loss → -entry*size.
        # entry_price = limit_price = 0.42 from _make_order defaults.
        entry = 0.42
        size = 200.0
        pnl = (1.0 - entry) * size if is_win else -entry * size
        settlements.append(_make_settlement(
            client_order_id=oid,
            outcome=outcome,
            realized_pnl=pnl,
        ))
    return orders, fills, settlements


def _hand_calibrated_honesty_fixture() -> tuple[
    list[PaperOrderRecord], list[PaperFillRecord], list[PaperSettlementRecord]
]:
    """Three buckets with hand-known honesty values, asserted to 4 decimals.

    Bucket assignments and arithmetic:

    * "1.0-1.5%": one order with adjusted_edge=0.012, theoretical_edge=0.020,
      realized_pnl=+10 → return = 10/200 = 0.050
      mean(return)=0.050, mean(theoretical)=0.020, honesty = 2.5000

    * "2-3%": two orders both with adjusted_edge=0.025, theoretical_edge=0.025
      one realized_pnl=+10 (return=0.050), one realized_pnl=-2 (return=-0.010)
      mean(return)=0.020, mean(theoretical)=0.025, honesty = 0.8000

    * "5-10%": one order with adjusted_edge=0.07, theoretical_edge=0.0
      → honesty = None (zero denominator)
    """
    orders = [
        _make_order(client_order_id="hc-1", adjusted_edge=0.012),
        _make_order(client_order_id="hc-2", adjusted_edge=0.025),
        _make_order(client_order_id="hc-3", adjusted_edge=0.025),
        _make_order(client_order_id="hc-4", adjusted_edge=0.07),
    ]
    fills = [_make_fill(client_order_id=oid) for oid in
             ("hc-1", "hc-2", "hc-3", "hc-4")]
    settlements = [
        # "1.0-1.5%" bucket: theoretical_edge=0.020, realized=+10 → return=0.05
        _make_settlement(
            client_order_id="hc-1", outcome="win", realized_pnl=10.0,
        ).model_copy(update={"theoretical_edge": 0.020}),
        # "2-3%" bucket: two settled rows
        _make_settlement(
            client_order_id="hc-2", outcome="win", realized_pnl=10.0,
        ).model_copy(update={"theoretical_edge": 0.025}),
        _make_settlement(
            client_order_id="hc-3", outcome="loss", realized_pnl=-2.0,
        ).model_copy(update={"theoretical_edge": 0.025}),
        # "5-10%" bucket: theoretical_edge=0.0 → honesty undefined
        _make_settlement(
            client_order_id="hc-4", outcome="win", realized_pnl=15.0,
        ).model_copy(update={"theoretical_edge": 0.0}),
    ]
    return orders, fills, settlements


# ── Test 1: empty bucket handled ─────────────────────────────────────────────


class TestEmptyBucketHandled:
    """A bucket with zero settled rows must produce a placeholder
    BucketSummary row (no division-by-zero, no exception)."""

    def test_empty_bucket_in_ordered_set_emits_placeholder(self) -> None:
        """When ``bucket_order`` includes a label with no observations,
        the row is emitted with n=0 and ci_method='n=0', not dropped."""
        orders = [_make_order(client_order_id="ord-1", adjusted_edge=0.025)]
        fills = [_make_fill(client_order_id="ord-1")]
        df = build_joined_dataframe(orders, fills, [])
        rows = bucket_summary(
            df, "conservative_edge_bucket",
            bucket_order=["1.0-1.5%", "2-3%", "5-10%"],
        )
        by_bucket = {r.bucket: r for r in rows}
        # 1.0-1.5% and 5-10% have no rows → placeholders
        assert by_bucket["1.0-1.5%"].n == 0
        assert by_bucket["1.0-1.5%"].ci_method == "n=0"
        assert by_bucket["1.0-1.5%"].mean_return is None
        assert by_bucket["1.0-1.5%"].honesty_ratio is None
        assert by_bucket["5-10%"].n == 0
        # 2-3% has the one row
        assert by_bucket["2-3%"].n == 1

    def test_unsettled_only_bucket_has_zero_eligible(self) -> None:
        """Bucket has rows but none are settled → n_eligible=0,
        ci_method='n=0', honesty_ratio=None."""
        orders = [_make_order(client_order_id=f"ord-{i}", adjusted_edge=0.025)
                  for i in range(3)]
        fills = [_make_fill(client_order_id=f"ord-{i}") for i in range(3)]
        df = build_joined_dataframe(orders, fills, [])  # no settlements
        rows = bucket_summary(df, "conservative_edge_bucket")
        # The 2-3% bucket has 3 rows but 0 settled
        target = next(r for r in rows if r.bucket == "2-3%")
        assert target.n == 3
        assert target.n_settled == 0
        assert target.n_eligible == 0
        assert target.mean_return is None
        assert target.honesty_ratio is None
        assert target.ci_method == "n=0"


# ── Test 2: single-record bucket no bootstrap crash ──────────────────────────


class TestSingleRecordBucketNoBootstrapCrash:
    """N=1 must return a degenerate CI (low==high==point estimate)
    with ci_method='degenerate_n=1', not raise."""

    def test_n1_bootstrap_returns_degenerate(self) -> None:
        low, high, method = bootstrap_mean_ci(np.array([0.025]))
        assert low == 0.025
        assert high == 0.025
        assert method == "degenerate_n=1"

    def test_n1_bucket_summary_no_crash(self) -> None:
        """One settled row → bucket summary produces ci_method
        'degenerate_n=1' for that bucket, no exception raised."""
        orders = [_make_order(client_order_id="ord-1", adjusted_edge=0.025)]
        fills = [_make_fill(client_order_id="ord-1")]
        settlements = [
            _make_settlement(client_order_id="ord-1", realized_pnl=10.0)
        ]
        df = build_joined_dataframe(orders, fills, settlements)
        rows = bucket_summary(df, "conservative_edge_bucket")
        target = next(r for r in rows if r.bucket == "2-3%")
        assert target.n == 1
        assert target.n_eligible == 1
        assert target.ci_low == target.ci_high
        assert target.ci_method == "degenerate_n=1"

    def test_zero_variance_bucket_no_crash(self) -> None:
        """Multiple rows with identical returns → ci_method
        'degenerate_zero_variance', bounds equal point estimate."""
        # All settled with realized_pnl=10 → return=0.05 for every row
        orders = [_make_order(client_order_id=f"ord-{i}", adjusted_edge=0.025)
                  for i in range(3)]
        fills = [_make_fill(client_order_id=f"ord-{i}") for i in range(3)]
        settlements = [
            _make_settlement(client_order_id=f"ord-{i}", realized_pnl=10.0)
            for i in range(3)
        ]
        df = build_joined_dataframe(orders, fills, settlements)
        rows = bucket_summary(df, "conservative_edge_bucket")
        target = next(r for r in rows if r.bucket == "2-3%")
        assert target.ci_method == "degenerate_zero_variance"
        assert target.ci_low == target.ci_high == pytest.approx(0.05)


# ── Test 3: render_all creates the charts directory ──────────────────────────


class TestRenderCreatesCharts:
    """Library-direct: invoke render_all on a synthetic frame and
    assert the charts/ subdirectory has the expected 21 PNGs."""

    def test_charts_dir_populated_with_21_pngs(self, tmp_path: Path) -> None:
        orders, fills, settlements = _synthesize_settled_ledger(n=50)
        df = build_joined_dataframe(orders, fills, settlements)

        out_dir = tmp_path / "out"
        report_mod.render_all(df, out_dir, ledger_dir="synthetic")

        charts_dir = out_dir / "charts"
        assert charts_dir.exists()
        png_files = sorted(charts_dir.glob("*.png"))
        # 5 dimensions × 4 charts = 20, plus 1 logit forest = 21.
        assert len(png_files) == 21
        # All non-empty (a 0-byte PNG would mean savefig wrote nothing).
        for p in png_files:
            assert p.stat().st_size > 0, f"{p.name} is empty"

    def test_chart_filenames_match_grep_convention(self, tmp_path: Path) -> None:
        """Every chart's name matches ``<dim>__<kind>.png`` so a
        report-side ``rg`` finds them all."""
        orders, fills, settlements = _synthesize_settled_ledger(n=40)
        df = build_joined_dataframe(orders, fills, settlements)

        out_dir = tmp_path / "out"
        report_mod.render_all(df, out_dir, ledger_dir="synthetic")
        names = {p.name for p in (out_dir / "charts").glob("*.png")}

        for dim in ("conservative_edge", "vol_regime_clean", "match_quality",
                    "max_feed_staleness_ms", "confidence"):
            for kind in ("histogram", "bucket_mean_returns",
                         "honesty_ratio", "win_rate"):
                expected = f"{dim}__{kind}.png"
                assert expected in names, f"missing chart: {expected}"
        assert "logit__forest_plot.png" in names


# ── Test 4: render_all creates summary_stats.json with expected keys ─────────


class TestRenderCreatesSummaryStats:
    """Library-direct: invoke render_all and parse summary_stats.json,
    assert the top-level shape matches the documented schema."""

    _EXPECTED_TOP_LEVEL_KEYS = {
        "schema_version",
        "generated_at",
        "ledger_dir",
        "dataset_window",
        "counts",
        "tier_reached",
        "schema_skips",
        "top_line_stats",
        "bucket_summaries",
        "regime_x_edge_crosstab",
        "logit_coefficients",
        "open_positions",
        "recommendations",
    }

    _EXPECTED_BUCKET_DIM_KEYS = {
        "conservative_edge",
        "vol_regime_clean",
        "match_quality",
        "max_feed_staleness_ms",
        "confidence",
    }

    def test_summary_stats_top_level_shape(self, tmp_path: Path) -> None:
        orders, fills, settlements = _synthesize_settled_ledger(n=50)
        df = build_joined_dataframe(orders, fills, settlements)

        out_dir = tmp_path / "out"
        report_mod.render_all(df, out_dir, ledger_dir="synthetic")

        path = out_dir / "summary_stats.json"
        assert path.exists()
        summary = json.loads(path.read_text(encoding="utf-8"))
        assert set(summary.keys()) == self._EXPECTED_TOP_LEVEL_KEYS
        assert summary["schema_version"] == 1
        assert set(summary["bucket_summaries"].keys()) == self._EXPECTED_BUCKET_DIM_KEYS
        # Each dimension has the per-dim schema.
        for dim in self._EXPECTED_BUCKET_DIM_KEYS:
            entry = summary["bucket_summaries"][dim]
            assert "bucket_kind" in entry
            assert "boundaries" in entry
            assert "rows" in entry
            assert isinstance(entry["rows"], list)

    def test_summary_returned_dict_matches_file(self, tmp_path: Path) -> None:
        """render_all returns the same dict it writes to disk."""
        orders, fills, settlements = _synthesize_settled_ledger(n=40)
        df = build_joined_dataframe(orders, fills, settlements)

        out_dir = tmp_path / "out"
        returned = report_mod.render_all(df, out_dir, ledger_dir="synthetic")
        on_disk = json.loads((out_dir / "summary_stats.json").read_text(encoding="utf-8"))
        # Top-level keys identical (string-coerced datetime equality is
        # the gotcha we'd have to coerce around for full deep-equality).
        assert set(returned.keys()) == set(on_disk.keys())
        assert returned["counts"] == on_disk["counts"]
        assert returned["tier_reached"] == on_disk["tier_reached"]


# ── Test 5: logit fits on minimal dataset ────────────────────────────────────


class TestLogitRunsOnMinimalDataset:
    """fit_logit must succeed on ~40 settled rows with feature
    variation, producing a coefficient table with the expected shape."""

    def test_logit_fits_with_40_settled_rows(self) -> None:
        orders, fills, settlements = _synthesize_settled_ledger(n=40, win_rate=0.55)
        df = build_joined_dataframe(orders, fills, settlements)

        result = fit_logit(df)
        assert result.fitted, f"logit failed: reason={result.reason}"
        assert result.features is not None
        # 6 numeric features + 2 vol_regime dummies (3 levels - 1 reference)
        # + 1 intercept (const) = 9 entries in the coefficient table.
        assert len(result.features) == 9
        assert any(f.name == "const" for f in result.features)
        assert any(f.name == "adjusted_edge" for f in result.features)
        assert any(f.name == "confidence" for f in result.features)
        # Quality flag is "indicative" because n=40 is below
        # TIER_MULTI_FEATURE (1500).
        assert result.quality_flag == "indicative"

    def test_logit_summary_serialization(self, tmp_path: Path) -> None:
        """Fitted logit lands in summary_stats.json with the structured
        quality_flag field — 9c's reader needs it programmatically."""
        orders, fills, settlements = _synthesize_settled_ledger(n=40, win_rate=0.55)
        df = build_joined_dataframe(orders, fills, settlements)

        out_dir = tmp_path / "out"
        report_mod.render_all(df, out_dir, ledger_dir="synthetic")
        summary = json.loads((out_dir / "summary_stats.json").read_text(encoding="utf-8"))
        logit_blob = summary["logit_coefficients"]
        assert logit_blob["fitted"] is True
        assert "quality_flag" in logit_blob
        assert logit_blob["quality_flag"] == "indicative"
        assert isinstance(logit_blob["features"], list)
        assert len(logit_blob["features"]) == 9


# ── Test 6: per-bucket honesty (hand-calibrated) ─────────────────────────────


class TestHonestyRatioPerBucket:
    """Hand-calibrated arithmetic asserted to 4 decimals."""

    def test_pure_honesty_function_matches_hand_calc(self) -> None:
        """honesty_ratio() against hand-known input."""
        returns = np.array([0.05])
        theoretical = np.array([0.020])
        # mean(0.05) / mean(0.020) = 2.5 exactly
        assert honesty_ratio(returns, theoretical) == pytest.approx(2.5, abs=1e-4)

    def test_honesty_ratio_two_bucket(self) -> None:
        """The "2-3%" bucket has hand-known honesty 0.8000 exactly."""
        returns = np.array([0.05, -0.01])  # 10/200, -2/200
        theoretical = np.array([0.025, 0.025])
        # mean(0.05, -0.01) / mean(0.025, 0.025) = 0.02 / 0.025 = 0.8
        assert honesty_ratio(returns, theoretical) == pytest.approx(0.8, abs=1e-4)

    def test_honesty_zero_denominator_returns_none(self) -> None:
        """mean(theoretical) == 0 → None, not inf."""
        assert honesty_ratio(np.array([0.05]), np.array([0.0])) is None

    def test_per_bucket_honesty_via_bucket_summary(self) -> None:
        """End-to-end: hand-calibrated ledger → bucket_summary → assert
        per-bucket honesty values to 4 decimals."""
        orders, fills, settlements = _hand_calibrated_honesty_fixture()
        df = build_joined_dataframe(orders, fills, settlements)
        rows = bucket_summary(df, "conservative_edge_bucket")
        by_bucket = {r.bucket: r for r in rows}

        assert by_bucket["1.0-1.5%"].honesty_ratio == pytest.approx(2.5, abs=1e-4)
        assert by_bucket["2-3%"].honesty_ratio == pytest.approx(0.8, abs=1e-4)
        # "5-10%": theoretical_edge=0 → undefined → None
        assert by_bucket["5-10%"].honesty_ratio is None


# ── Test 7: logit skipped on single-class outcome ────────────────────────────


class TestLogitSkippedOnSingleClass:
    """All-wins (or all-losses) settled subset → fit_logit returns
    ``fitted=False, reason='single_class_outcome'``."""

    def test_all_wins_returns_skip_with_reason(self) -> None:
        orders = [_make_order(client_order_id=f"ord-{i}",
                              adjusted_edge=0.02 + 0.001 * i)
                  for i in range(40)]
        fills = [_make_fill(client_order_id=f"ord-{i}") for i in range(40)]
        # All wins → single-class target.
        settlements = [
            _make_settlement(client_order_id=f"ord-{i}",
                             outcome="win", realized_pnl=58.0)
            for i in range(40)
        ]
        df = build_joined_dataframe(orders, fills, settlements)

        result = fit_logit(df)
        assert result.fitted is False
        assert result.reason == "single_class_outcome"


# ── Test 8: regime × edge crosstab gated below tier ──────────────────────────


class TestRegimeCrosstabGatedBelowTier:
    """N_settled < TIER_REGIME_CONDITIONAL → summary_stats.json's
    regime_x_edge_crosstab carries gated=True with the threshold info."""

    def test_crosstab_gated_at_low_n(self, tmp_path: Path) -> None:
        n = 100  # well below TIER_REGIME_CONDITIONAL (800)
        assert n < TIER_REGIME_CONDITIONAL
        orders, fills, settlements = _synthesize_settled_ledger(n=n)
        df = build_joined_dataframe(orders, fills, settlements)

        out_dir = tmp_path / "out"
        summary = report_mod.render_all(df, out_dir, ledger_dir="synthetic")

        ct = summary["regime_x_edge_crosstab"]
        assert ct["gated"] is True
        assert ct["tier_required"] == "regime_conditional"
        assert ct["n_settled"] == n
        assert ct["n_required"] == TIER_REGIME_CONDITIONAL
        assert ct["rows"] is None


# ── Test 9: indicative flag propagates below threshold ───────────────────────


class TestIndicativeFlagPropagates:
    """Below TIER_SINGLE_THRESHOLD per bucket → all bucket rows carry
    ``quality_flag == "indicative"``; the recommendations engine sees
    only indicative buckets and emits no recommendations regardless of
    honesty values."""

    def test_all_buckets_indicative_below_threshold(self, tmp_path: Path) -> None:
        n = 50
        assert n < TIER_SINGLE_THRESHOLD
        orders, fills, settlements = _synthesize_settled_ledger(n=n)
        df = build_joined_dataframe(orders, fills, settlements)
        rows = bucket_summary(df, "conservative_edge_bucket")
        for r in rows:
            # Every bucket is below the threshold (50 total settled rows
            # spread across 7 buckets is necessarily < 300 in any bucket)
            assert r.quality_flag == "indicative"

    def test_no_recommendations_emitted_for_indicative_buckets(self) -> None:
        """Even with honesty < 0.75, an indicative-only bucket must
        not emit a recommendation — gating is the load-bearing
        invariant the rule's conservatism rests on."""
        # Hand-build a single bucket with low honesty but indicative flag.
        bad_indicative = BucketSummary(
            bucket="1.0-1.5%",
            n=20, n_settled=20, n_eligible=20,
            win_rate=0.4,
            mean_return=0.005,
            ci_low=0.001, ci_high=0.009, ci_method="BCa",
            mean_theoretical_edge=0.012,
            honesty_ratio=0.42,
            quality_flag="indicative",
        )
        recs = generate_recommendations([bad_indicative])
        assert recs == []

    def test_recommendation_emitted_for_calibration_bucket_below_threshold(self) -> None:
        """The complement: a bucket with quality_flag='calibration' AND
        honesty < 0.75 DOES emit one raise_edge_floor recommendation,
        evidence cites the bucket label."""
        good_calibration_low_honesty = BucketSummary(
            bucket="1.0-1.5%",
            n=400, n_settled=400, n_eligible=400,
            win_rate=0.4,
            mean_return=0.005,
            ci_low=0.001, ci_high=0.009, ci_method="BCa",
            mean_theoretical_edge=0.012,
            honesty_ratio=0.42,
            quality_flag="calibration",
        )
        recs = generate_recommendations([good_calibration_low_honesty])
        assert len(recs) == 1
        rec = recs[0]
        assert rec.kind == "raise_edge_floor"
        assert rec.quality_flag == "calibration"
        assert "1.0-1.5%" in rec.evidence
        # Suggested upper boundary for "1.0-1.5%" is "1.5%".
        assert "1.5%" in rec.suggested_action

    def test_no_recommendation_when_honesty_above_threshold(self) -> None:
        """quality_flag='calibration' but honesty >= 0.75 → no rec."""
        ok_bucket = BucketSummary(
            bucket="2-3%",
            n=400, n_settled=400, n_eligible=400,
            win_rate=0.55,
            mean_return=0.018,
            ci_low=0.012, ci_high=0.024, ci_method="BCa",
            mean_theoretical_edge=0.022,
            honesty_ratio=0.82,
            quality_flag="calibration",
        )
        recs = generate_recommendations([ok_bucket])
        assert recs == []
        # Sanity-check the threshold is what we think it is.
        assert HONESTY_RECOMMEND_THRESHOLD == 0.75
