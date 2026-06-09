"""Tests for tools/analyze_paper_ledger.py — Round 9b1 scope.

Covers the data-pipeline foundation: JSONL loading with skip-and-warn,
vol_regime normalization, conservative-edge bucketing, and the
three-way join that produces the analysis DataFrame.

Statistical analyses, charts, and the markdown report are tested in
9b2's additions — this file does not exercise those paths.

All tests use synthetic in-memory fixtures written to ``tmp_path``.
None of them touch the live ``./paper_ledger/`` directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from btc_pm_arb.execution.paper_ledger import (
    PaperFillRecord,
    PaperOrderRecord,
    PaperRejectionRecord,
    PaperSettlementRecord,
)
from btc_pm_arb.models import DataSource
from tools.analyze_paper_ledger import (
    assess_power_tier,
    assign_conservative_edge_bucket,
    assign_fill_adjusted_edge_bucket,
    build_joined_dataframe,
    chase_adjusted_band_distribution,
    fill_adjusted_band_distribution,
    filter_as_of,
    load_jsonl,
    main,
    normalize_vol_regime,
)


# ── Fixture builders ─────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=7)


def _make_order(
    client_order_id: str = "ord-1",
    *,
    adjusted_edge: float = 0.13,
    vol_regime: str = "VolRegime.LOW",
    confidence: float = 0.7,
    match_quality: float = 0.95,
    feed_staleness_ms: dict[str, float | None] | None = None,
    expiry: datetime | None = None,
    created_at: datetime | None = None,
) -> PaperOrderRecord:
    return PaperOrderRecord(
        client_order_id=client_order_id,
        signal_fingerprint=f"fp-{client_order_id}",
        created_at=created_at or _NOW,
        platform=DataSource.KALSHI,
        contract_id="KXBTC-26MAY01-B100000",
        side="yes",
        size_usd=200.0,
        limit_price=0.42,
        raw_edge=0.13,
        adjusted_edge=adjusted_edge,
        fill_adjusted_edge=None,
        confidence=confidence,
        vol_regime=vol_regime,
        feed_staleness_ms=(
            feed_staleness_ms
            if feed_staleness_ms is not None
            else {"deribit": 100.0, "kalshi": 200.0, "polymarket": 150.0}
        ),
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=match_quality,
        expiry=expiry or _EXPIRY,
    )


def _make_fill(
    client_order_id: str = "ord-1",
    *,
    outcome: str = "full",
    fill_price: float = 0.42,
    fill_size_usd: float = 200.0,
    fees_usd: float = 0.0,
) -> PaperFillRecord:
    return PaperFillRecord(
        client_order_id=client_order_id,
        filled_at=_NOW,
        fill_price=fill_price if outcome != "no_fill" else None,
        fill_size_usd=fill_size_usd if outcome != "no_fill" else 0.0,
        fill_outcome=outcome,  # type: ignore[arg-type]
        simulator_reason="ok",
        fees_usd=fees_usd,
    )


def _make_settlement(
    client_order_id: str = "ord-1",
    *,
    outcome: str = "win",
    realized_pnl: float = 100.0,
    settlement_price: float = 1.0,
    side: str = "yes",
    settled_at: datetime | None = None,
) -> PaperSettlementRecord:
    payout_price = settlement_price if side == "yes" else 1.0 - settlement_price
    return PaperSettlementRecord(
        client_order_id=client_order_id,
        contract_id="KXBTC-26MAY01-B100000",
        platform=DataSource.KALSHI,
        side=side,  # type: ignore[arg-type]
        settled_at=settled_at or (_NOW + timedelta(days=8)),
        settlement_price=settlement_price,
        payout_price=payout_price,
        entry_price=0.42,
        size_usd=200.0,
        realized_pnl=realized_pnl,
        fees_usd=0.0,
        outcome=outcome,  # type: ignore[arg-type]
        theoretical_edge=0.13,
        expiry=_EXPIRY,
    )


def _write_jsonl(path: Path, lines: list[str]) -> None:
    """Write raw JSONL lines verbatim — used for malformed-data tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line)
            if not line.endswith("\n"):
                f.write("\n")


def _write_records(path: Path, records: list) -> None:
    """Write valid pydantic records as JSONL via ``model_dump_json``."""
    _write_jsonl(path, [r.model_dump_json() for r in records])


def _make_rejection(
    *,
    reason_key: str = "conservative_edge",
    best_conservative_edge: float = 0.02,
    fill_adjusted_edge: float | None = None,
    fill_outcome: str | None = None,
    fill_simulator_reason: str | None = None,
    fill_size_usd: float | None = None,
    chase_adjusted_edge: float | None = None,
    run_id: str = "",
    contract_id: str = "KX-REJ-1",
) -> PaperRejectionRecord:
    return PaperRejectionRecord(
        timestamp=_NOW,
        contract_id=contract_id,
        platform=DataSource.KALSHI,
        reason_key=reason_key,
        full_reason=f"{reason_key} detail",
        best_conservative_edge=best_conservative_edge,
        vol_regime="normal",
        fill_adjusted_edge=fill_adjusted_edge,
        fill_outcome=fill_outcome,  # type: ignore[arg-type]
        fill_simulator_reason=fill_simulator_reason,
        fill_size_usd=fill_size_usd,
        chase_adjusted_edge=chase_adjusted_edge,
        run_id=run_id,
    )


# ── Test 5: vol_regime normalization (pure unit) ─────────────────────────────


class TestNormalizeVolRegime:
    """Round 9b1 normalizes the on-disk ``"VolRegime.LOW"`` shape into
    a clean lowercase label, idempotent on already-clean values."""

    def test_strips_volregime_prefix(self) -> None:
        assert normalize_vol_regime("VolRegime.LOW") == "low"
        assert normalize_vol_regime("VolRegime.NORMAL") == "normal"
        assert normalize_vol_regime("VolRegime.HIGH") == "high"

    def test_idempotent_on_clean_values(self) -> None:
        # If a future producer-side cleanup emits "low" / "normal" /
        # "high" directly, the normalizer must not break.
        assert normalize_vol_regime("low") == "low"
        assert normalize_vol_regime("normal") == "normal"
        assert normalize_vol_regime("high") == "high"

    def test_handles_uppercase_clean(self) -> None:
        # Defensive: lowercase regardless of source-side conventions.
        assert normalize_vol_regime("LOW") == "low"
        assert normalize_vol_regime("HIGH") == "high"

    def test_none_returns_unknown(self) -> None:
        assert normalize_vol_regime(None) == "unknown"


# ── Test 6: bucket assignment boundaries (pure unit) ─────────────────────────


class TestAssignConservativeEdgeBucket:
    """Bucket boundaries are left-inclusive; verifies each boundary
    point falls in the correct bin and out-of-range values map to
    ``below_min`` / ``unknown`` rather than crashing."""

    def test_below_floor(self) -> None:
        assert assign_conservative_edge_bucket(0.005) == "below_min"
        assert assign_conservative_edge_bucket(0.0099) == "below_min"
        assert assign_conservative_edge_bucket(0.0) == "below_min"
        assert assign_conservative_edge_bucket(-0.05) == "below_min"

    def test_left_inclusive_boundaries(self) -> None:
        # Each lower edge of a bucket falls IN that bucket.
        assert assign_conservative_edge_bucket(0.010) == "1.0-1.5%"
        assert assign_conservative_edge_bucket(0.015) == "1.5-2%"
        assert assign_conservative_edge_bucket(0.020) == "2-3%"
        assert assign_conservative_edge_bucket(0.030) == "3-5%"
        assert assign_conservative_edge_bucket(0.050) == "5-10%"
        assert assign_conservative_edge_bucket(0.100) == "10%+"

    def test_just_below_upper_boundary(self) -> None:
        # Just below an upper boundary stays in the lower bucket.
        assert assign_conservative_edge_bucket(0.0149) == "1.0-1.5%"
        assert assign_conservative_edge_bucket(0.0299) == "2-3%"
        assert assign_conservative_edge_bucket(0.0999) == "5-10%"

    def test_above_top_threshold(self) -> None:
        # Final bucket extends to infinity.
        assert assign_conservative_edge_bucket(0.5) == "10%+"
        assert assign_conservative_edge_bucket(1.0) == "10%+"

    def test_none_returns_unknown(self) -> None:
        assert assign_conservative_edge_bucket(None) == "unknown"


# ── Test 3 & 4: schema_version skip + invalid-JSON skip (loader) ────────────


class TestLoadJsonlSkipAndWarn:
    """Round 9b1's loader uses skip-and-warn-with-counter on every kind
    of bad data: malformed JSON, unknown schema_version, pydantic
    validation failures.  Surrounding records must continue to load."""

    def test_unknown_schema_version_skipped(self, tmp_path: Path) -> None:
        """Records with ``schema_version != 1`` are skipped, counted,
        and the surrounding valid records still load."""
        good_1 = _make_order(client_order_id="ord-good-1")
        good_2 = _make_order(client_order_id="ord-good-2")
        # Build a phantom-schema record by mutating the JSON payload of a
        # valid record (round-trip + tweak; we deliberately bypass the
        # pydantic constructor because the schema_version field is
        # frozen-by-default at validation time).
        bad_payload = json.loads(good_1.model_dump_json())
        bad_payload["client_order_id"] = "ord-future-schema"
        bad_payload["schema_version"] = 99

        orders_path = tmp_path / "orders.jsonl"
        _write_jsonl(
            orders_path,
            [
                good_1.model_dump_json(),
                json.dumps(bad_payload),
                good_2.model_dump_json(),
            ],
        )

        result = load_jsonl(orders_path, PaperOrderRecord)
        assert len(result.records) == 2
        assert {r.client_order_id for r in result.records} == {
            "ord-good-1", "ord-good-2",
        }
        assert result.n_skipped_unknown_schema == 1
        assert result.n_skipped_invalid == 0

    def test_invalid_json_skipped(self, tmp_path: Path) -> None:
        """Malformed JSON lines are skipped, counted, and surrounding
        valid records still load."""
        good_1 = _make_order(client_order_id="ord-good-1")
        good_2 = _make_order(client_order_id="ord-good-2")

        orders_path = tmp_path / "orders.jsonl"
        _write_jsonl(
            orders_path,
            [
                good_1.model_dump_json(),
                "not json at all{{{{{",
                good_2.model_dump_json(),
            ],
        )

        result = load_jsonl(orders_path, PaperOrderRecord)
        assert len(result.records) == 2
        assert {r.client_order_id for r in result.records} == {
            "ord-good-1", "ord-good-2",
        }
        assert result.n_skipped_invalid == 1
        assert result.n_skipped_unknown_schema == 0

    def test_pydantic_validation_failure_skipped(self, tmp_path: Path) -> None:
        """Records with the right schema_version but a field that fails
        pydantic validation are counted as ``n_skipped_invalid`` (NOT
        ``n_skipped_unknown_schema``)."""
        good = _make_order(client_order_id="ord-good")
        # Mutate a constrained field to an invalid value.
        bad_payload = json.loads(good.model_dump_json())
        bad_payload["client_order_id"] = "ord-bad"
        # limit_price is constrained to [0, 1] — set it to 5.0
        bad_payload["limit_price"] = 5.0

        orders_path = tmp_path / "orders.jsonl"
        _write_jsonl(
            orders_path,
            [good.model_dump_json(), json.dumps(bad_payload)],
        )

        result = load_jsonl(orders_path, PaperOrderRecord)
        assert len(result.records) == 1
        assert result.records[0].client_order_id == "ord-good"
        assert result.n_skipped_invalid == 1
        assert result.n_skipped_unknown_schema == 0

    def test_blank_lines_silently_skipped(self, tmp_path: Path) -> None:
        """Blank lines (truncated-trailing-line resilience) are skipped
        without incrementing either counter — matches paper_ledger.py
        reader policy."""
        good = _make_order(client_order_id="ord-good")
        orders_path = tmp_path / "orders.jsonl"
        _write_jsonl(orders_path, ["", good.model_dump_json(), "", ""])

        result = load_jsonl(orders_path, PaperOrderRecord)
        assert len(result.records) == 1
        assert result.n_skipped_invalid == 0
        assert result.n_skipped_unknown_schema == 0

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """A missing JSONL file produces an empty LoadResult, not an
        exception — a fresh paper_ledger directory may have only some
        of the three files."""
        result = load_jsonl(tmp_path / "does_not_exist.jsonl", PaperOrderRecord)
        assert result.records == []
        assert result.n_skipped_invalid == 0
        assert result.n_skipped_unknown_schema == 0


# ── Test 1: three-way join correctness ───────────────────────────────────────


class TestThreeWayJoin:
    def test_join_correctly_marks_settled_and_open(self) -> None:
        """3 orders, 3 fills, 2 settlements → joined frame has 3 rows
        with 2 ``is_settled=True`` and 1 ``is_settled=False``."""
        orders = [
            _make_order(client_order_id=f"ord-{i}") for i in range(1, 4)
        ]
        fills = [
            _make_fill(client_order_id=f"ord-{i}") for i in range(1, 4)
        ]
        settlements = [
            _make_settlement(client_order_id="ord-1", realized_pnl=120.0),
            _make_settlement(client_order_id="ord-2", realized_pnl=-80.0),
            # ord-3 is open — no settlement
        ]
        df = build_joined_dataframe(orders, fills, settlements)

        assert len(df) == 3
        assert df["is_settled"].sum() == 2
        # ord-3 is open
        ord3 = df[df["client_order_id"] == "ord-3"].iloc[0]
        assert not ord3["is_settled"]

    def test_settled_rows_have_realized_pnl(self) -> None:
        """Settled rows carry the realized_pnl from the settlement record."""
        orders = [_make_order(client_order_id="ord-1")]
        fills = [_make_fill(client_order_id="ord-1")]
        settlements = [
            _make_settlement(client_order_id="ord-1", realized_pnl=120.0)
        ]
        df = build_joined_dataframe(orders, fills, settlements)
        assert df.iloc[0]["realized_pnl"] == pytest.approx(120.0)
        assert df.iloc[0]["return"] == pytest.approx(120.0 / 200.0)

    def test_open_rows_have_nan_return(self) -> None:
        """Unsettled rows have NaN in ``return`` — distinct from 0.0."""
        orders = [_make_order(client_order_id="ord-1")]
        fills = [_make_fill(client_order_id="ord-1")]
        settlements: list[PaperSettlementRecord] = []
        df = build_joined_dataframe(orders, fills, settlements)
        assert pd.isna(df.iloc[0]["return"])
        assert not df.iloc[0]["is_settled"]

    def test_derived_columns_present(self) -> None:
        """The four derived columns appear regardless of settlement state."""
        orders = [_make_order(client_order_id="ord-1")]
        fills = [_make_fill(client_order_id="ord-1")]
        df = build_joined_dataframe(orders, fills, [])
        for col in [
            "is_settled",
            "vol_regime_clean",
            "max_feed_staleness_ms",
            "conservative_edge_bucket",
            "return",
        ]:
            assert col in df.columns, f"missing derived column: {col}"

    def test_vol_regime_normalized_in_join(self) -> None:
        """The ``vol_regime_clean`` column carries the prefix-stripped
        lowercase label, regardless of source-side serialization."""
        orders = [
            _make_order(client_order_id="ord-low", vol_regime="VolRegime.LOW"),
            _make_order(client_order_id="ord-clean", vol_regime="normal"),
            _make_order(client_order_id="ord-high", vol_regime="VolRegime.HIGH"),
        ]
        df = build_joined_dataframe(orders, [], [])
        # Set indexed by client_order_id for stable lookup.
        df_by_id = df.set_index("client_order_id")
        assert df_by_id.loc["ord-low", "vol_regime_clean"] == "low"
        assert df_by_id.loc["ord-clean", "vol_regime_clean"] == "normal"
        assert df_by_id.loc["ord-high", "vol_regime_clean"] == "high"

    def test_max_feed_staleness_computed(self) -> None:
        """``max_feed_staleness_ms`` returns the max of non-None entries."""
        orders = [
            _make_order(
                client_order_id="ord-1",
                feed_staleness_ms={"deribit": 100.0, "kalshi": 500.0, "polymarket": 250.0},
            ),
            _make_order(
                client_order_id="ord-2",
                feed_staleness_ms={"deribit": 50.0, "kalshi": None, "polymarket": 75.0},
            ),
        ]
        df = build_joined_dataframe(orders, [], [])
        df_by_id = df.set_index("client_order_id")
        assert df_by_id.loc["ord-1", "max_feed_staleness_ms"] == 500.0
        assert df_by_id.loc["ord-2", "max_feed_staleness_ms"] == 75.0

    def test_per_feed_staleness_columns_expanded(self) -> None:
        """The dict ``feed_staleness_ms`` column is replaced by three
        flat scalars (parquet-safety + per-feed analysis prep)."""
        orders = [
            _make_order(
                client_order_id="ord-1",
                feed_staleness_ms={"deribit": 100.0, "kalshi": 500.0, "polymarket": 250.0},
            ),
            _make_order(
                client_order_id="ord-2",
                feed_staleness_ms={"deribit": 50.0, "kalshi": None, "polymarket": 75.0},
            ),
        ]
        df = build_joined_dataframe(orders, [], [])

        # Original nested column dropped.
        assert "feed_staleness_ms" not in df.columns
        # Three per-feed scalars present.
        for col in ("deribit_staleness_ms", "kalshi_staleness_ms", "polymarket_staleness_ms"):
            assert col in df.columns

        df_by_id = df.set_index("client_order_id")
        assert df_by_id.loc["ord-1", "deribit_staleness_ms"] == 100.0
        assert df_by_id.loc["ord-1", "kalshi_staleness_ms"] == 500.0
        assert df_by_id.loc["ord-1", "polymarket_staleness_ms"] == 250.0
        # None values preserved as NaN/None per pandas object-column semantics.
        assert df_by_id.loc["ord-2", "deribit_staleness_ms"] == 50.0
        assert pd.isna(df_by_id.loc["ord-2", "kalshi_staleness_ms"])
        assert df_by_id.loc["ord-2", "polymarket_staleness_ms"] == 75.0

    def test_order_book_columns_dropped(self) -> None:
        """Order-book list columns are dropped from the joined frame
        (parquet-safety; depth analysis is a future round's concern)."""
        orders = [_make_order(client_order_id="ord-1")]
        df = build_joined_dataframe(orders, [], [])
        assert "order_book_yes" not in df.columns
        assert "order_book_no" not in df.columns

    def test_conservative_edge_bucket_assigned(self) -> None:
        """The bucket label column reflects ``adjusted_edge``."""
        orders = [
            _make_order(client_order_id="ord-1", adjusted_edge=0.013),  # 1.0-1.5%
            _make_order(client_order_id="ord-2", adjusted_edge=0.025),  # 2-3%
            _make_order(client_order_id="ord-3", adjusted_edge=0.15),   # 10%+
        ]
        df = build_joined_dataframe(orders, [], [])
        df_by_id = df.set_index("client_order_id")
        assert df_by_id.loc["ord-1", "conservative_edge_bucket"] == "1.0-1.5%"
        assert df_by_id.loc["ord-2", "conservative_edge_bucket"] == "2-3%"
        assert df_by_id.loc["ord-3", "conservative_edge_bucket"] == "10%+"

    def test_empty_input_returns_empty_frame_with_columns(self) -> None:
        """Empty input → empty frame WITH derived columns present so
        downstream code can address them safely."""
        df = build_joined_dataframe([], [], [])
        assert df.empty
        for col in [
            "client_order_id",
            "is_settled",
            "vol_regime_clean",
            "max_feed_staleness_ms",
            "conservative_edge_bucket",
            "return",
        ]:
            assert col in df.columns


# ── Build step 4: fill-fidelity (slippage) + P&L columns ─────────────────────


class TestFillFidelityColumns:
    """The join emits a slippage column (fill_price - limit_price) alongside
    the realized-P&L columns -- the fill-fidelity surface build step 4 adds."""

    def test_slippage_is_fill_minus_limit(self) -> None:
        # limit_price 0.42; the book-walk filled better at 0.40 -> -0.02.
        orders = [_make_order(client_order_id="ord-1")]
        fills = [_make_fill(client_order_id="ord-1", fill_price=0.40)]
        df = build_joined_dataframe(orders, fills, [])
        assert "slippage" in df.columns
        assert df.iloc[0]["slippage"] == pytest.approx(0.40 - 0.42)

    def test_slippage_zero_when_filled_at_limit(self) -> None:
        orders = [_make_order(client_order_id="ord-1")]
        fills = [_make_fill(client_order_id="ord-1", fill_price=0.42)]
        df = build_joined_dataframe(orders, fills, [])
        assert df.iloc[0]["slippage"] == pytest.approx(0.0)

    def test_slippage_nan_when_no_fill(self) -> None:
        """An order with no fill row has NaN slippage (no fill_price)."""
        orders = [_make_order(client_order_id="ord-1")]
        df = build_joined_dataframe(orders, [], [])
        assert "slippage" in df.columns
        assert pd.isna(df.iloc[0]["slippage"])

    def test_join_emits_slippage_and_pnl_together(self) -> None:
        """A settled order carries BOTH the slippage and the realized-P&L
        fill-fidelity columns in the same joined row."""
        orders = [_make_order(client_order_id="ord-1")]
        fills = [_make_fill(client_order_id="ord-1", fill_price=0.40)]
        settlements = [_make_settlement(client_order_id="ord-1", realized_pnl=116.0)]
        df = build_joined_dataframe(orders, fills, settlements)
        row = df.iloc[0]
        assert row["slippage"] == pytest.approx(0.40 - 0.42)
        assert row["realized_pnl"] == pytest.approx(116.0)
        assert row["return"] == pytest.approx(116.0 / 200.0)

    def test_run_id_and_mode_ride_through_join(self) -> None:
        """run_id / mode columns survive the join so runs stay separable."""
        orders = [_make_order(client_order_id="ord-1")]
        df = build_joined_dataframe(orders, [], [])
        assert "run_id" in df.columns
        assert "mode" in df.columns

    def test_empty_frame_has_slippage_column(self) -> None:
        df = build_joined_dataframe([], [], [])
        assert "slippage" in df.columns


# ── Test 2: missing-fill handling ────────────────────────────────────────────


class TestJoinHandlesMissingFill:
    """Orders without corresponding fills are uncommon (the Round 8
    fill simulator records a fill on every placement, including
    ``no_fill`` outcomes).  But the join must not lose the order row
    when a fill happens to be missing — both the order's own fields
    and the derived columns must still be present."""

    def test_order_without_fill_still_appears(self) -> None:
        orders = [
            _make_order(client_order_id="ord-with-fill"),
            _make_order(client_order_id="ord-no-fill"),
        ]
        fills = [_make_fill(client_order_id="ord-with-fill")]
        settlements: list[PaperSettlementRecord] = []
        df = build_joined_dataframe(orders, fills, settlements)

        assert len(df) == 2
        assert set(df["client_order_id"]) == {"ord-with-fill", "ord-no-fill"}
        # The fill-less order still has order-side columns.
        no_fill_row = df[df["client_order_id"] == "ord-no-fill"].iloc[0]
        assert no_fill_row["adjusted_edge"] == pytest.approx(0.13)
        assert no_fill_row["match_quality"] == pytest.approx(0.95)
        assert not no_fill_row["is_settled"]
        # And derived columns are populated from order fields.
        assert no_fill_row["vol_regime_clean"] == "low"

    def test_no_fills_at_all_still_loads_orders(self) -> None:
        """Edge case: zero fills, all orders are placement-only."""
        orders = [_make_order(client_order_id=f"ord-{i}") for i in range(3)]
        df = build_joined_dataframe(orders, [], [])
        assert len(df) == 3
        assert (~df["is_settled"]).all()


# ── Test 10: open-positions excluded from P&L analysis ───────────────────────


class TestOpenPositionsExcludedFromPnL:
    """The joined frame includes both settled and open orders, but
    P&L statistics must be computed only over the settled set so the
    ``return`` column on open rows stays NaN (distinct from 0.0)."""

    def test_split_settled_vs_open(self) -> None:
        orders = [
            _make_order(client_order_id="settled-1"),
            _make_order(client_order_id="settled-2"),
            _make_order(client_order_id="settled-3"),
            _make_order(client_order_id="open-1"),
            _make_order(client_order_id="open-2"),
        ]
        fills = [_make_fill(client_order_id=oid) for oid in [
            "settled-1", "settled-2", "settled-3", "open-1", "open-2",
        ]]
        settlements = [
            _make_settlement(client_order_id="settled-1", realized_pnl=100.0),
            _make_settlement(client_order_id="settled-2", realized_pnl=-50.0),
            _make_settlement(client_order_id="settled-3", realized_pnl=20.0),
        ]
        df = build_joined_dataframe(orders, fills, settlements)

        # Five orders total; three settled, two open.
        assert len(df) == 5
        assert df["is_settled"].sum() == 3
        assert (~df["is_settled"]).sum() == 2

    def test_open_returns_are_nan(self) -> None:
        """Open positions have NaN return; settled positions have
        computed returns from realized_pnl / size_usd."""
        orders = [
            _make_order(client_order_id="settled-1"),
            _make_order(client_order_id="open-1"),
        ]
        fills = [
            _make_fill(client_order_id="settled-1"),
            _make_fill(client_order_id="open-1"),
        ]
        settlements = [
            _make_settlement(client_order_id="settled-1", realized_pnl=100.0),
        ]
        df = build_joined_dataframe(orders, fills, settlements)

        open_returns = df[~df["is_settled"]]["return"]
        settled_returns = df[df["is_settled"]]["return"]
        assert open_returns.isna().all()
        assert not settled_returns.isna().any()
        assert settled_returns.iloc[0] == pytest.approx(100.0 / 200.0)

    def test_pnl_aggregation_excludes_open(self) -> None:
        """Sum of realized_pnl over the settled subset matches expectation;
        open positions don't pollute the total because their realized_pnl
        is NaN, and pandas .sum() skips NaN by default."""
        orders = [
            _make_order(client_order_id=oid)
            for oid in ["settled-1", "settled-2", "settled-3", "open-1", "open-2"]
        ]
        fills = [_make_fill(client_order_id=oid) for oid in [
            "settled-1", "settled-2", "settled-3", "open-1", "open-2",
        ]]
        settlements = [
            _make_settlement(client_order_id="settled-1", realized_pnl=100.0),
            _make_settlement(client_order_id="settled-2", realized_pnl=-50.0),
            _make_settlement(client_order_id="settled-3", realized_pnl=20.0),
        ]
        df = build_joined_dataframe(orders, fills, settlements)

        # Total realized P&L over the settled subset.
        settled_pnl = df[df["is_settled"]]["realized_pnl"].sum()
        assert settled_pnl == pytest.approx(70.0)
        # The .sum() on the full column also gives 70 (NaN skipped),
        # but the analysis surface should always filter explicitly so
        # the open subset never leaks into rate computations.


# ── filter_as_of: point-in-time analysis ─────────────────────────────────────


class TestFilterAsOf:
    """``--as-of`` enables point-in-time analysis: orders created after
    ``as_of`` are dropped; orders that settled after ``as_of`` remain in
    the frame but with settlement-side fields nulled, so the analysis
    sees the dataset as it would have looked at ``as_of``."""

    def test_late_settlements_zeroed_to_open(self) -> None:
        """5 settlements at staggered timestamps; with as_of mid-range,
        pre-as_of settlements remain settled and post-as_of settlements
        are zeroed out (treated as still-open)."""
        as_of = _NOW + timedelta(days=10)
        orders = [
            _make_order(client_order_id=f"ord-{i}") for i in range(1, 6)
        ]
        fills = [_make_fill(client_order_id=f"ord-{i}") for i in range(1, 6)]
        # Pre-as_of: ord-1 (day 5), ord-2 (day 8) → still settled
        # Post-as_of: ord-3 (day 12), ord-4 (day 15) → zeroed
        # Never settled: ord-5
        settlements = [
            _make_settlement(
                client_order_id="ord-1",
                settled_at=_NOW + timedelta(days=5),
                realized_pnl=100.0,
            ),
            _make_settlement(
                client_order_id="ord-2",
                settled_at=_NOW + timedelta(days=8),
                realized_pnl=-50.0,
            ),
            _make_settlement(
                client_order_id="ord-3",
                settled_at=_NOW + timedelta(days=12),
                realized_pnl=20.0,
            ),
            _make_settlement(
                client_order_id="ord-4",
                settled_at=_NOW + timedelta(days=15),
                realized_pnl=80.0,
            ),
        ]
        df = build_joined_dataframe(orders, fills, settlements)
        filtered = filter_as_of(df, as_of)

        # All 5 orders remain (created_at = _NOW which is before as_of).
        assert len(filtered) == 5
        df_by_id = filtered.set_index("client_order_id")

        # Pre-as_of settlements: still settled, returns intact.
        assert df_by_id.loc["ord-1", "is_settled"]
        assert df_by_id.loc["ord-2", "is_settled"]
        assert df_by_id.loc["ord-1", "return"] == pytest.approx(100.0 / 200.0)
        assert df_by_id.loc["ord-2", "return"] == pytest.approx(-50.0 / 200.0)

        # Post-as_of settlements: zeroed back to open.
        assert not df_by_id.loc["ord-3", "is_settled"]
        assert not df_by_id.loc["ord-4", "is_settled"]
        assert pd.isna(df_by_id.loc["ord-3", "return"])
        assert pd.isna(df_by_id.loc["ord-4", "return"])

        # Never-settled: still open.
        assert not df_by_id.loc["ord-5", "is_settled"]
        assert pd.isna(df_by_id.loc["ord-5", "return"])

    def test_drops_orders_created_after_as_of(self) -> None:
        """Orders with ``created_at > as_of`` are dropped from the frame
        entirely — they didn't exist yet at the analysis instant."""
        early = _make_order(client_order_id="ord-early", created_at=_NOW)
        late = _make_order(
            client_order_id="ord-late",
            created_at=_NOW + timedelta(days=20),
        )
        df = build_joined_dataframe([early, late], [], [])
        as_of = _NOW + timedelta(days=10)
        filtered = filter_as_of(df, as_of)

        assert len(filtered) == 1
        assert filtered.iloc[0]["client_order_id"] == "ord-early"

    def test_none_as_of_is_noop(self) -> None:
        """Passing ``None`` for ``as_of`` returns the frame unchanged."""
        orders = [_make_order(client_order_id=f"ord-{i}") for i in range(3)]
        df = build_joined_dataframe(orders, [], [])
        out = filter_as_of(df, None)
        assert len(out) == len(df)
        assert list(out["client_order_id"]) == list(df["client_order_id"])


# ── assess_power_tier: tier-label gates ──────────────────────────────────────


class TestAssessPowerTier:
    """Power-tier labels gate which calibration model classes 9c is
    allowed to recommend (single-threshold / regime-conditional /
    multi-feature).  Boundary values must produce stable labels because
    9c reads the tier from ``summary_stats.json`` and dispatches on it."""

    def test_tier_labels_at_canonical_n_settled_values(self) -> None:
        assert assess_power_tier(50) == "below_single_threshold"
        assert assess_power_tier(350) == "single_threshold"
        assert assess_power_tier(1000) == "regime_conditional"
        assert assess_power_tier(2000) == "multi_feature"

    def test_tier_boundaries_are_left_inclusive(self) -> None:
        """At each threshold, the higher tier kicks in (>= comparison)."""
        # Just below / at / above each boundary.
        assert assess_power_tier(299) == "below_single_threshold"
        assert assess_power_tier(300) == "single_threshold"
        assert assess_power_tier(799) == "single_threshold"
        assert assess_power_tier(800) == "regime_conditional"
        assert assess_power_tier(1499) == "regime_conditional"
        assert assess_power_tier(1500) == "multi_feature"

    def test_zero_n_settled(self) -> None:
        """Empty dataset → below_single_threshold."""
        assert assess_power_tier(0) == "below_single_threshold"


# ── End-to-end main() smoke test ─────────────────────────────────────────────


class TestMainEndToEnd:
    """Integration-shaped: writes a synthetic ledger to ``tmp_path``,
    invokes ``main()`` programmatically, and asserts the parquet
    round-trips with the expected shape.

    Catches pyarrow schema-inference bugs that pure-DataFrame tests
    miss — the per-feed expansion + order-book drop in
    ``build_joined_dataframe`` were specifically chosen to keep this
    path reliable, and this test is the canary that confirms the
    whole pipeline survives parquet serialization end-to-end."""

    def test_main_writes_parquet_and_round_trips(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "ledger"
        out_dir = tmp_path / "out"
        ledger_dir.mkdir()

        orders = [
            _make_order(client_order_id=f"ord-{i}", adjusted_edge=0.02 + 0.01 * i)
            for i in range(1, 4)
        ]
        fills = [_make_fill(client_order_id=f"ord-{i}") for i in range(1, 4)]
        settlements = [
            _make_settlement(client_order_id="ord-1", realized_pnl=100.0),
            _make_settlement(client_order_id="ord-2", realized_pnl=-50.0),
            # ord-3 is open
        ]
        _write_records(ledger_dir / "orders.jsonl", orders)
        _write_records(ledger_dir / "fills.jsonl", fills)
        _write_records(ledger_dir / "settlements.jsonl", settlements)

        rc = main(["--ledger-dir", str(ledger_dir), "--out-dir", str(out_dir)])
        assert rc == 0

        parquet_path = out_dir / "joined.parquet"
        assert parquet_path.exists()
        # Round-trip: read the parquet back and verify the expected shape.
        df = pd.read_parquet(parquet_path)
        assert len(df) == 3
        assert df["is_settled"].sum() == 2

        # Derived columns survive the parquet round-trip.
        for col in (
            "vol_regime_clean",
            "max_feed_staleness_ms",
            "deribit_staleness_ms",
            "kalshi_staleness_ms",
            "polymarket_staleness_ms",
            "conservative_edge_bucket",
            "return",
        ):
            assert col in df.columns, f"missing derived column after parquet round-trip: {col}"

        # Heavy nested columns dropped — confirm not in parquet either.
        assert "feed_staleness_ms" not in df.columns
        assert "order_book_yes" not in df.columns
        assert "order_book_no" not in df.columns

    def test_main_handles_empty_ledger(self, tmp_path: Path) -> None:
        """Fresh ledger directory → empty parquet, no crash."""
        ledger_dir = tmp_path / "ledger"
        out_dir = tmp_path / "out"
        ledger_dir.mkdir()

        rc = main(["--ledger-dir", str(ledger_dir), "--out-dir", str(out_dir)])
        assert rc == 0
        # Parquet file written even when source files are missing — keeps
        # downstream readers from having to special-case "no file yet."
        assert (out_dir / "joined.parquet").exists()
        df = pd.read_parquet(out_dir / "joined.parquet")
        assert df.empty

    def test_main_writes_report_and_summary_stats(self, tmp_path: Path) -> None:
        """9b2b: main() invokes render_all after the parquet write, so
        report.md, summary_stats.json, and the charts/ bundle land in
        out_dir alongside joined.parquet from a single CLI invocation."""
        # Cross-imported here (not at module level) to avoid 9b1 tests
        # paying the matplotlib import cost.  Future-cleanup candidate:
        # promote _synthesize_settled_ledger into tests/test_tools/conftest.py.
        from tests.test_tools.test_analyze_paper_ledger_9b2 import (
            _synthesize_settled_ledger,
        )

        ledger_dir = tmp_path / "ledger"
        out_dir = tmp_path / "out"
        ledger_dir.mkdir()

        # 40 settled rows: enough for fit_logit / bucket summaries to
        # exercise their non-degenerate paths (matches 9b2a precedent).
        orders, fills, settlements = _synthesize_settled_ledger(n=40)
        _write_records(ledger_dir / "orders.jsonl", orders)
        _write_records(ledger_dir / "fills.jsonl", fills)
        _write_records(ledger_dir / "settlements.jsonl", settlements)

        rc = main(["--ledger-dir", str(ledger_dir), "--out-dir", str(out_dir)])
        assert rc == 0

        # All four deliverables exist.
        assert (out_dir / "joined.parquet").exists()
        report_md = out_dir / "report.md"
        assert report_md.exists()
        assert report_md.stat().st_size > 0
        summary_json = out_dir / "summary_stats.json"
        assert summary_json.exists()
        charts_dir = out_dir / "charts"
        assert charts_dir.is_dir()
        # Canary chart — 9b2a's library-direct tests already cover the
        # full 21-PNG inventory; here we only confirm the chart pipeline
        # ran end-to-end through main().
        assert (charts_dir / "conservative_edge__histogram.png").exists()

        # summary_stats.json is well-formed and carries the documented
        # top-level keys.
        summary = json.loads(summary_json.read_text(encoding="utf-8"))
        assert summary["schema_version"] == 1
        for key in (
            "counts",
            "tier_reached",
            "bucket_summaries",
            "schema_skips",
            "logit_coefficients",
        ):
            assert key in summary, f"missing top-level key: {key}"

        # Schema-skip plumbing: synthetic-clean fixture → zero on both
        # counters for every file type.  Catches a future regression
        # that accidentally drops the schema_skips=... wiring.
        assert summary["schema_skips"]["orders"]["unknown_schema"] == 0
        assert summary["schema_skips"]["orders"]["invalid"] == 0
        assert summary["schema_skips"]["fills"]["unknown_schema"] == 0
        assert summary["schema_skips"]["settlements"]["unknown_schema"] == 0

    def test_main_propagates_render_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """9b2b error policy: render_all exceptions propagate out of main()
        as a CLI traceback rather than leaving a half-rendered out_dir.
        Also pins call ordering — joined.parquet must exist before
        render_all runs, so a render-side failure still leaves the
        parquet for re-analysis."""
        ledger_dir = tmp_path / "ledger"
        out_dir = tmp_path / "out"
        ledger_dir.mkdir()

        orders = [_make_order(client_order_id=f"ord-{i}") for i in range(1, 4)]
        fills = [_make_fill(client_order_id=f"ord-{i}") for i in range(1, 4)]
        _write_records(ledger_dir / "orders.jsonl", orders)
        _write_records(ledger_dir / "fills.jsonl", fills)
        _write_records(ledger_dir / "settlements.jsonl", [])

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        # Patch at the import site (where main() resolves the name),
        # not at tools.analysis.report.render_all.
        monkeypatch.setattr("tools.analyze_paper_ledger.render_all", _boom)

        with pytest.raises(RuntimeError, match="boom"):
            main(["--ledger-dir", str(ledger_dir), "--out-dir", str(out_dir)])

        # Parquet was written before the render explosion — the operator
        # can re-run analysis without re-collecting data.
        assert (out_dir / "joined.parquet").exists()


# ── Fill-adjusted band distribution (rejection-side measurement infra) ────────


class TestAssignFillAdjustedEdgeBucket:
    def test_negative_buckets(self) -> None:
        assert assign_fill_adjusted_edge_bucket(-0.20) == "<-5%"
        assert assign_fill_adjusted_edge_bucket(-0.05) == "-5to-2%"
        assert assign_fill_adjusted_edge_bucket(-0.03) == "-5to-2%"
        assert assign_fill_adjusted_edge_bucket(-0.02) == "-2to0%"
        assert assign_fill_adjusted_edge_bucket(-0.0001) == "-2to0%"

    def test_nonnegative_buckets(self) -> None:
        assert assign_fill_adjusted_edge_bucket(0.0) == "0-1%"
        assert assign_fill_adjusted_edge_bucket(0.009) == "0-1%"
        assert assign_fill_adjusted_edge_bucket(0.01) == "1-3%"
        assert assign_fill_adjusted_edge_bucket(0.029) == "1-3%"
        assert assign_fill_adjusted_edge_bucket(0.03) == "3-5%"
        assert assign_fill_adjusted_edge_bucket(0.05) == "5%+"
        assert assign_fill_adjusted_edge_bucket(0.5) == "5%+"

    def test_none_is_no_fill(self) -> None:
        assert assign_fill_adjusted_edge_bucket(None) == "no_fill"


class TestFillAdjustedBandDistribution:
    def test_only_walked_rejections_contribute(self) -> None:
        rejections = [
            # Walked: full fill, edge collapsed negative.
            _make_rejection(
                fill_adjusted_edge=-0.06, fill_outcome="full",
                best_conservative_edge=0.025,
            ),
            # Walked: partial, near-floor positive.
            _make_rejection(
                fill_adjusted_edge=0.012, fill_outcome="partial",
                best_conservative_edge=0.012,
            ),
            # Walked: no_fill (empty book) — no edge to bucket.
            _make_rejection(
                fill_adjusted_edge=None, fill_outcome="no_fill",
                fill_simulator_reason="empty_book", best_conservative_edge=0.02,
            ),
            # NOT walked (structural rejection outside the band).
            _make_rejection(reason_key="no_positive_edge", best_conservative_edge=0.0),
        ]
        band = fill_adjusted_band_distribution(rejections)

        assert band["n_rejections_total"] == 4
        assert band["n_walked"] == 3
        assert band["n_full"] == 1
        assert band["n_partial"] == 1
        assert band["n_no_fill"] == 1
        assert band["n_skipped"] == 1

        by_bucket = {row["bucket"]: row for row in band["buckets"]}
        assert by_bucket["<-5%"]["n"] == 1
        assert by_bucket["<-5%"]["mean_fill_adjusted_edge"] == pytest.approx(-0.06)
        assert by_bucket["<-5%"]["mean_best_conservative_edge"] == pytest.approx(0.025)
        assert by_bucket["1-3%"]["n"] == 1
        assert by_bucket["1-3%"]["n_partial"] == 1
        assert by_bucket["no_fill"]["n"] == 1
        assert by_bucket["no_fill"]["mean_fill_adjusted_edge"] is None

    def test_empty_input(self) -> None:
        band = fill_adjusted_band_distribution([])
        assert band["n_rejections_total"] == 0
        assert band["n_walked"] == 0
        # Every bucket present with zero count (stable schema for the campaign).
        assert all(row["n"] == 0 for row in band["buckets"])


class TestMainEmitsFillAdjustedBand:
    def test_main_writes_fill_adjusted_band_json(self, tmp_path: Path) -> None:
        """End-to-end: main() emits fill_adjusted_band.json summarising the
        book-walked near-floor rejections — the band that previously existed
        only as un-book-walked rejections."""
        from tests.test_tools.test_analyze_paper_ledger_9b2 import (
            _synthesize_settled_ledger,
        )

        ledger_dir = tmp_path / "ledger"
        out_dir = tmp_path / "out"
        ledger_dir.mkdir()

        orders, fills, settlements = _synthesize_settled_ledger(n=10)
        _write_records(ledger_dir / "orders.jsonl", orders)
        _write_records(ledger_dir / "fills.jsonl", fills)
        _write_records(ledger_dir / "settlements.jsonl", settlements)
        # Two book-walked near-floor rejections + one structural skip.
        _write_records(ledger_dir / "rejections.jsonl", [
            _make_rejection(
                fill_adjusted_edge=-0.06, fill_outcome="full",
                fill_simulator_reason="book_walk_full", best_conservative_edge=0.025,
            ),
            _make_rejection(
                fill_adjusted_edge=0.012, fill_outcome="partial",
                fill_simulator_reason="book_walk_partial", best_conservative_edge=0.012,
            ),
            _make_rejection(reason_key="no_positive_edge", best_conservative_edge=0.0),
        ])

        # --run-id all so the synthetic run-id scoping doesn't drop rejections.
        rc = main([
            "--ledger-dir", str(ledger_dir), "--out-dir", str(out_dir),
            "--run-id", "all",
        ])
        assert rc == 0

        band_path = out_dir / "fill_adjusted_band.json"
        assert band_path.exists()
        band = json.loads(band_path.read_text(encoding="utf-8"))
        assert band["n_rejections_total"] == 3
        assert band["n_walked"] == 2
        assert band["n_skipped"] == 1
        by_bucket = {row["bucket"]: row for row in band["buckets"]}
        assert by_bucket["<-5%"]["n"] == 1
        assert by_bucket["1-3%"]["n"] == 1


# ── Chase-adjusted band distribution (#1 UNCAPPED completion cost) ────────────


class TestChaseAdjustedBandDistribution:
    """``chase_adjusted_band_distribution`` buckets the UNCAPPED #1 edge across
    the full signed axis -- INCLUDING the negative buckets the capped #2 band
    never reaches -- and carries #2's partial-fill rate per bucket side by side.
    A negative bucket is a CORRECTLY-REJECTED LOSER, not a missed signal."""

    def test_negatives_land_in_negative_buckets_with_capped_rate_side_by_side(
        self,
    ) -> None:
        rejections = [
            # #1 chase collapsed deep negative; #2 capped stayed a positive
            # partial fill -- the two coexist and disagree by design.
            _make_rejection(
                chase_adjusted_edge=-0.17, fill_adjusted_edge=0.007,
                fill_outcome="partial", best_conservative_edge=0.007,
            ),
            # Another negative; #2 was a full fill here.
            _make_rejection(
                chase_adjusted_edge=-0.06, fill_adjusted_edge=0.02,
                fill_outcome="full", best_conservative_edge=0.025,
            ),
            # #1 stayed positive (deep book, no wall) -> non-negative bucket.
            _make_rejection(
                chase_adjusted_edge=0.012, fill_adjusted_edge=0.012,
                fill_outcome="full", best_conservative_edge=0.012,
            ),
            # #1 no_fill (whole book too thin to complete the size).
            _make_rejection(
                chase_adjusted_edge=None, fill_adjusted_edge=None,
                fill_outcome="no_fill", fill_simulator_reason="empty_book",
                best_conservative_edge=0.02,
            ),
            # NOT walked (structural rejection) -> excluded from the band.
            _make_rejection(
                reason_key="no_positive_edge", best_conservative_edge=0.0,
            ),
        ]
        chase = chase_adjusted_band_distribution(rejections)

        assert chase["n_walked"] == 4
        assert chase["n_chase_negative"] == 2
        assert chase["n_chase_nonneg"] == 1
        assert chase["n_chase_no_fill"] == 1
        # The labeling guardrail is stamped into the JSON so a future reader
        # cannot misread the negative buckets as missed signals.
        assert "CORRECTLY-REJECTED LOSER" in chase["legend"]

        by_bucket = {row["bucket"]: row for row in chase["buckets"]}
        # Both -0.17 and -0.06 fall in the "<-5%" bucket.
        assert by_bucket["<-5%"]["n"] == 2
        assert by_bucket["<-5%"]["mean_chase_adjusted_edge"] == pytest.approx(
            (-0.17 + -0.06) / 2
        )
        # #2's partial-fill rate per bucket, side by side: one of the two
        # negative-#1 rejections was a #2 partial fill.
        assert by_bucket["<-5%"]["n_partial"] == 1
        assert by_bucket["<-5%"]["partial_fill_rate"] == pytest.approx(0.5)
        assert by_bucket["<-5%"]["mean_fill_adjusted_edge"] == pytest.approx(
            (0.007 + 0.02) / 2
        )
        # The positive #1 lands in the 1-3% bucket (never clipped/merged).
        assert by_bucket["1-3%"]["n"] == 1
        assert by_bucket["1-3%"]["mean_chase_adjusted_edge"] == pytest.approx(0.012)
        # no_fill #1 -> the "no_fill" bucket, no edge to place.
        assert by_bucket["no_fill"]["n"] == 1
        assert by_bucket["no_fill"]["mean_chase_adjusted_edge"] is None

    def test_negative_is_reported_as_is_never_clipped_to_zero(self) -> None:
        """Anti-phantom: a negative #1 edge is reported verbatim -- the band
        must never floor it to look better."""
        chase = chase_adjusted_band_distribution([
            _make_rejection(
                chase_adjusted_edge=-0.2003, fill_adjusted_edge=0.006,
                fill_outcome="partial", best_conservative_edge=0.006,
            ),
        ])
        by_bucket = {row["bucket"]: row for row in chase["buckets"]}
        assert by_bucket["<-5%"]["mean_chase_adjusted_edge"] == pytest.approx(-0.2003)
        assert chase["n_chase_negative"] == 1

    def test_empty_input(self) -> None:
        chase = chase_adjusted_band_distribution([])
        assert chase["n_walked"] == 0
        assert chase["n_chase_negative"] == 0
        # Every bucket present with zero count (stable schema for the campaign).
        assert all(row["n"] == 0 for row in chase["buckets"])


class TestMainEmitsChaseBand:
    def test_main_nests_chase_band_alongside_capped_band(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end: main() nests the #1 chase band under the same
        fill_adjusted_band.json sidecar as the #2 band (summary_stats.json
        untouched), with the negatives in the negative buckets."""
        from tests.test_tools.test_analyze_paper_ledger_9b2 import (
            _synthesize_settled_ledger,
        )

        ledger_dir = tmp_path / "ledger"
        out_dir = tmp_path / "out"
        ledger_dir.mkdir()

        orders, fills, settlements = _synthesize_settled_ledger(n=10)
        _write_records(ledger_dir / "orders.jsonl", orders)
        _write_records(ledger_dir / "fills.jsonl", fills)
        _write_records(ledger_dir / "settlements.jsonl", settlements)
        _write_records(ledger_dir / "rejections.jsonl", [
            # #1 negative (loser) while #2 capped stayed a positive partial.
            _make_rejection(
                chase_adjusted_edge=-0.17, fill_adjusted_edge=0.007,
                fill_outcome="partial", fill_simulator_reason="book_walk_partial",
                best_conservative_edge=0.007,
            ),
            # #1 positive near-floor.
            _make_rejection(
                chase_adjusted_edge=0.012, fill_adjusted_edge=0.012,
                fill_outcome="full", fill_simulator_reason="book_walk_full",
                best_conservative_edge=0.012,
            ),
            _make_rejection(
                reason_key="no_positive_edge", best_conservative_edge=0.0,
            ),
        ])

        rc = main([
            "--ledger-dir", str(ledger_dir), "--out-dir", str(out_dir),
            "--run-id", "all",
        ])
        assert rc == 0

        band = json.loads(
            (out_dir / "fill_adjusted_band.json").read_text(encoding="utf-8")
        )
        # The #2 band is still present and unchanged alongside the new chase.
        assert band["n_walked"] == 2
        assert "chase" in band
        chase = band["chase"]
        assert chase["n_walked"] == 2
        assert chase["n_chase_negative"] == 1
        assert "CORRECTLY-REJECTED LOSER" in chase["legend"]
        by_bucket = {row["bucket"]: row for row in chase["buckets"]}
        assert by_bucket["<-5%"]["n"] == 1
        assert by_bucket["<-5%"]["partial_fill_rate"] == pytest.approx(1.0)
        assert by_bucket["1-3%"]["n"] == 1

        # summary_stats.json schema is untouched (no chase keys leaked in).
        summary = json.loads(
            (out_dir / "summary_stats.json").read_text(encoding="utf-8")
        )
        assert "chase" not in summary
