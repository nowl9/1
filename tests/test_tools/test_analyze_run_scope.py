"""C3 regression: analyzer N_settled=0 guard + latest-run_id scoping."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from tools.analysis.stats import fit_logit
from tools.analyze_paper_ledger import latest_run_id, main, build_joined_dataframe

from tests.test_tools.test_analyze_paper_ledger import (
    _NOW,
    _make_fill,
    _make_order,
)


# ── C3a: fit_logit no longer KeyErrors on a no-settlement frame ───────────────

def test_fit_logit_handles_zero_settled_without_keyerror():
    # Orders + fills but NO settlements -> joined frame has no "outcome" column.
    orders = [_make_order(client_order_id=f"ord-{i}") for i in range(3)]
    fills = [_make_fill(client_order_id=f"ord-{i}") for i in range(3)]
    df = build_joined_dataframe(orders, fills, [])
    assert "outcome" not in df.columns          # precondition for the old crash
    result = fit_logit(df)                       # must NOT raise KeyError
    assert result.fitted is False
    assert result.reason == "insufficient_settled_rows"
    assert result.n == 0


# ── C3b: latest_run_id ────────────────────────────────────────────────────────

def test_latest_run_id_picks_max_created_at():
    stale = _make_order(client_order_id="old").model_copy(
        update={"run_id": "stale", "created_at": _NOW - timedelta(days=30)}
    )
    fresh = _make_order(client_order_id="new").model_copy(
        update={"run_id": "fresh", "created_at": _NOW}
    )
    assert latest_run_id([stale, fresh]) == "fresh"
    assert latest_run_id([]) is None


# ── C3b: main() scopes to the latest run by default, --run-id all opts out ────

def _write_jsonl(path, records):
    path.write_text(
        "\n".join(r.model_dump_json() for r in records) + "\n", encoding="utf-8"
    )


def _seed_two_runs(ledger_dir):
    stale = [
        _make_order(client_order_id=f"s{i}").model_copy(
            update={"run_id": "stale", "created_at": _NOW - timedelta(days=30)}
        )
        for i in range(2)
    ]
    fresh = [
        _make_order(client_order_id="f0").model_copy(
            update={"run_id": "fresh", "created_at": _NOW}
        )
    ]
    fills = [
        _make_fill(client_order_id=o.client_order_id).model_copy(
            update={"run_id": o.run_id}
        )
        for o in stale + fresh
    ]
    _write_jsonl(ledger_dir / "orders.jsonl", stale + fresh)
    _write_jsonl(ledger_dir / "fills.jsonl", fills)


def test_main_defaults_to_latest_run(tmp_path):
    ledger = tmp_path / "ledger"; ledger.mkdir()
    out = tmp_path / "out"
    _seed_two_runs(ledger)
    rc = main(["--ledger-dir", str(ledger), "--out-dir", str(out)])
    assert rc == 0
    df = pd.read_parquet(out / "joined.parquet")
    assert len(df) == 1                          # only the fresh run
    assert set(df["run_id"]) == {"fresh"}


def test_main_run_id_all_includes_every_run(tmp_path):
    ledger = tmp_path / "ledger"; ledger.mkdir()
    out = tmp_path / "out"
    _seed_two_runs(ledger)
    rc = main(["--ledger-dir", str(ledger), "--out-dir", str(out), "--run-id", "all"])
    assert rc == 0
    df = pd.read_parquet(out / "joined.parquet")
    assert len(df) == 3                          # stale (2) + fresh (1)
    assert set(df["run_id"]) == {"stale", "fresh"}
