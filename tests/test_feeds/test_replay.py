"""Replay reader + cold-path smoke (build step 5).

Two layers:

* Fast synthetic-fixture unit tests for the reader mechanics that do NOT need
  a fitted vol surface: gzip parse through the LIVE normalizer, ``advance_to``
  driving the clock, book->case join by token_id, and the no-network
  guarantee.
* The cold-path smoke against the REAL recordings (data/recordings) -- the
  seeded-cache-debt retirement.  From an EMPTY surface, ``--mode replay``
  reproduces signal -> fill -> ledger -> settlement, two cold replays are
  identical on the semantic projection (determinism decision (b)), and the
  jump-to-expiry is what makes settlement fire.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from btc_pm_arb.clock import SimulatedClock
from btc_pm_arb.feeds.replay import ReplayReader, _book_levels
from btc_pm_arb.main import Agent

# Real recordings used by the cold-path smoke.  Skip (not fail) when a
# checkout lacks the gitignored capture so the rest of the suite stays green.
_REC_DIR = Path("data/recordings")
_REC_DATE = "2026-05-30"
_HAS_RECORDINGS = (
    (_REC_DIR / "deribit" / _REC_DATE / "frames-20.jsonl.gz").exists()
    and (_REC_DIR / "polymarket" / _REC_DATE / "frames-20.jsonl.gz").exists()
)
_needs_recordings = pytest.mark.skipif(
    not _HAS_RECORDINGS, reason="real recordings (data/recordings) not present"
)


# ── Synthetic recording helpers ───────────────────────────────────────────────


def _write_gz(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _deribit_frame(ts: str, instrument: str, strike: float) -> dict:
    inner = {
        "method": "subscription",
        "params": {
            "channel": f"ticker.{instrument}.100ms",
            "data": {
                "instrument_name": instrument,
                "mark_iv": 45.0,
                "mark_price": 0.05,
                "best_bid_price": 0.04,
                "best_ask_price": 0.06,
                "underlying_price": 73900.0,
                "index_price": 73900.0,
                "timestamp": 1_900_000_000_000,
            },
        },
    }
    return {"ts": ts, "source": "deribit", "endpoint": None, "frame": json.dumps(inner)}


def _pm_markets_frame(ts: str, token_yes: str) -> dict:
    market = {
        "active": True,
        "closed": False,
        "question": "Will the price of Bitcoin be above $70,000 on May 31?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps([token_yes, "TOKEN_NO"]),
        "endDate": "2026-05-31T16:00:00Z",
        "id": "mkt-1",
    }
    return {
        "ts": ts, "source": "polymarket", "endpoint": "/markets?active=true",
        "frame": json.dumps([market]),
    }


def _pm_book_frame(ts: str, token_yes: str) -> dict:
    book = {
        "bids": [{"price": "0.95", "size": "120"}, {"price": "0.94", "size": "80"}],
        "asks": [{"price": "0.96", "size": "100"}, {"price": "0.97", "size": "50"}],
    }
    return {
        "ts": ts, "source": "polymarket",
        "endpoint": f"/book?token_id={token_yes}", "frame": json.dumps(book),
    }


def _make_replay_agent(tmp_path: Path, monkeypatch, *, min_edge: float = 0.01) -> Agent:
    monkeypatch.setattr("btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path))
    monkeypatch.setattr("btc_pm_arb.config.settings.min_edge", min_edge)
    clock = SimulatedClock(
        "replay", start=datetime(2026, 5, 30, tzinfo=timezone.utc)
    )
    return Agent(dry_run=True, clock=clock, run_id="testrun")


# ── _book_levels unit ─────────────────────────────────────────────────────────


def test_book_levels_parses_and_sorts_ascending():
    raw = [{"price": "0.97", "size": "50"}, {"price": "0.96", "size": "100"}]
    assert _book_levels(raw) == [(0.96, 100.0), (0.97, 50.0)]
    assert _book_levels(None) == []
    assert _book_levels([{"price": "bad"}]) == []


# ── Reader mechanics (synthetic, fast) ────────────────────────────────────────


async def test_reader_parses_gzip_via_normalizer_and_advances_clock(
    tmp_path, monkeypatch,
):
    """Reader reads gzipped JSONL through the LIVE parse paths and drives the
    clock off the recorded ``ts`` via ``advance_to`` -- no second parser."""
    rec = tmp_path / "recordings"
    _write_gz(
        rec / "deribit" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            _deribit_frame("2026-05-30T20:00:01+00:00", "BTC-31MAY26-70000-C", 70000),
            _deribit_frame("2026-05-30T20:00:02+00:00", "BTC-31MAY26-72000-C", 72000),
        ],
    )
    _write_gz(
        rec / "polymarket" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            _pm_markets_frame("2026-05-30T20:00:00+00:00", "TOKEN_YES"),
            _pm_book_frame("2026-05-30T20:00:03+00:00", "TOKEN_YES"),
        ],
    )
    agent = _make_replay_agent(tmp_path / "ledger", monkeypatch)
    reader = ReplayReader(
        record_dir=rec, date="2026-05-30", agent=agent,
        sources=("deribit", "polymarket"), jump_to_expiry=False,
    )
    stats = await reader.run()

    # Live parse paths ran: two OptionTicks + one PM tick (the /book joined to
    # the /markets case).
    assert stats["deribit_ticks"] == 2
    assert stats["pm_ticks"] == 1
    # advance_to drove the clock to the last (merged) recorded ts.
    assert agent.clock.now() == datetime(
        2026, 5, 30, 20, 0, 3, tzinfo=timezone.utc
    )
    # The Deribit ticks reached the surface through the real normalizer path.
    assert len(agent.surface.all_expiries()) >= 1


async def test_book_frame_dropped_when_token_untracked(tmp_path, monkeypatch):
    """A /book frame whose token_id has no /markets case is dropped (join by
    token_id, Carried Gap b) -- not parsed against a guessed market."""
    rec = tmp_path / "recordings"
    _write_gz(
        rec / "polymarket" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            # /book for a token that never appeared in a /markets disc frame.
            _pm_book_frame("2026-05-30T20:00:03+00:00", "UNTRACKED_TOKEN"),
        ],
    )
    agent = _make_replay_agent(tmp_path / "ledger", monkeypatch)
    reader = ReplayReader(
        record_dir=rec, date="2026-05-30", agent=agent,
        sources=("polymarket",), jump_to_expiry=False,
    )
    stats = await reader.run()
    assert stats["pm_ticks"] == 0


async def test_book_frame_joined_when_token_tracked(tmp_path, monkeypatch):
    """The same /book frame IS ingested once its token_id is tracked via a
    preceding /markets disc frame."""
    rec = tmp_path / "recordings"
    _write_gz(
        rec / "polymarket" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            _pm_markets_frame("2026-05-30T20:00:00+00:00", "TOKEN_YES"),
            _pm_book_frame("2026-05-30T20:00:03+00:00", "TOKEN_YES"),
        ],
    )
    agent = _make_replay_agent(tmp_path / "ledger", monkeypatch)
    reader = ReplayReader(
        record_dir=rec, date="2026-05-30", agent=agent,
        sources=("polymarket",), jump_to_expiry=False,
    )
    stats = await reader.run()
    assert stats["pm_ticks"] == 1


async def test_reader_positions_an_unanchored_clock_from_first_frame(
    tmp_path, monkeypatch,
):
    """run() builds the replay clock UNANCHORED (no start); the reader must
    position it from the first recorded frame and track sim-time -- not stay
    frozen at a wall-clock anchor ahead of the recordings (regression: a
    wall-clock anchor froze the clock and made every tick hours-stale)."""
    rec = tmp_path / "recordings"
    _write_gz(
        rec / "deribit" / "2026-05-30" / "frames-20.jsonl.gz",
        [_deribit_frame("2026-05-30T20:00:01+00:00", "BTC-31MAY26-70000-C", 70000)],
    )
    _write_gz(
        rec / "polymarket" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            _pm_markets_frame("2026-05-30T20:00:00+00:00", "TOKEN_YES"),
            _pm_book_frame("2026-05-30T20:00:03+00:00", "TOKEN_YES"),
        ],
    )
    monkeypatch.setattr(
        "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path / "ledger")
    )
    monkeypatch.setattr("btc_pm_arb.config.settings.min_edge", 0.01)
    # Unanchored replay clock, exactly as main.run() constructs it.
    agent = Agent(dry_run=True, clock=SimulatedClock("replay"), run_id="testrun")
    reader = ReplayReader(
        record_dir=rec, date="2026-05-30", agent=agent,
        sources=("deribit", "polymarket"), jump_to_expiry=False,
    )
    stats = await reader.run()
    assert stats["pm_ticks"] == 1
    # Clock tracked the recorded timeline, not wall-clock.
    assert agent.clock.now() == datetime(
        2026, 5, 30, 20, 0, 3, tzinfo=timezone.utc
    )


async def test_reader_tolerates_truncated_gzip_tail(tmp_path, monkeypatch):
    """A recording killed mid-write leaves a truncated / corrupt gzip member.

    Regression: ``_source_lines`` read the file with no guard around the
    decompressor, so a corrupt tail raised ``zlib.error`` partway through the
    k-way merge and crashed the whole run at window-end -- BEFORE the terminal
    scan -- losing every frame in the window.  The reader must keep the frames
    decoded before the break point and skip the rest of that file.
    """
    rec = tmp_path / "recordings"
    dpath = rec / "deribit" / "2026-05-30" / "frames-20.jsonl.gz"
    _write_gz(
        dpath,
        [
            _deribit_frame("2026-05-30T20:00:01+00:00", "BTC-31MAY26-70000-C", 70000),
            _deribit_frame("2026-05-30T20:00:02+00:00", "BTC-31MAY26-72000-C", 72000),
        ],
    )
    # Append a corrupt trailing member: gzip magic + garbage deflate bytes, so
    # the decompressor raises only AFTER the two valid frames are yielded.
    with open(dpath, "ab") as fh:
        fh.write(b"\x1f\x8b\x08\x00" + b"\xff" * 64)
    _write_gz(
        rec / "polymarket" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            _pm_markets_frame("2026-05-30T20:00:00+00:00", "TOKEN_YES"),
            _pm_book_frame("2026-05-30T20:00:03+00:00", "TOKEN_YES"),
        ],
    )
    agent = _make_replay_agent(tmp_path / "ledger", monkeypatch)
    reader = ReplayReader(
        record_dir=rec, date="2026-05-30", agent=agent,
        sources=("deribit", "polymarket"), jump_to_expiry=False,
    )
    stats = await reader.run()   # must NOT raise on the corrupt tail
    # Both pre-corruption Deribit frames survived; the PM file read cleanly.
    assert stats["deribit_ticks"] == 2
    assert stats["pm_ticks"] == 1


async def test_run_fires_scans_at_live_5s_cadence_not_terminal(tmp_path, monkeypatch):
    """C1: run() fires scans at 5 s sim-boundaries as frames advance -- not a
    single terminal scan.

    A PM tick arriving early in a window LONGER than the 300 s
    ``max_data_age_seconds`` cutoff must be SCANNED FRESH (age <= one interval)
    instead of being staled out as ``pm_data_age`` at window-end.  Exercises the
    REAL ``run()`` scan path by spying on ``agent.run_scan_pipeline`` (the
    clock-anchor lesson: assert on what run() actually scans, with the sim-clock
    it advances -- not the reader in isolation).
    """
    from datetime import timedelta as _td

    from btc_pm_arb.feeds.replay import _SCAN_INTERVAL

    t0 = datetime(2026, 5, 30, 20, 0, 0, tzinfo=timezone.utc)

    def iso(sec: int) -> str:
        return (t0 + _td(seconds=sec)).isoformat()

    rec = tmp_path / "recordings"
    # Deribit frames spread across a 360 s window (> the 300 s cutoff) drive the
    # clock across the 5 s boundaries.
    _write_gz(
        rec / "deribit" / "2026-05-30" / "frames-20.jsonl.gz",
        [_deribit_frame(iso(s), "BTC-31MAY26-70000-C", 70000) for s in range(0, 361, 30)],
    )
    # PM contract appears at t0 and books at t0+1 s -- early in the window.
    _write_gz(
        rec / "polymarket" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            _pm_markets_frame(iso(0), "TOKEN_YES"),
            _pm_book_frame(iso(1), "TOKEN_YES"),
        ],
    )
    agent = _make_replay_agent(tmp_path / "ledger", monkeypatch)

    # Spy on the REAL scan pipeline: record (scan_sim_time, [pm tick ts]).
    calls: list[tuple[datetime, list[datetime]]] = []
    orig = agent.run_scan_pipeline

    async def _spy(pm_ticks):
        calls.append((agent.clock.now(), [t.timestamp for t in pm_ticks]))
        return await orig(pm_ticks)

    monkeypatch.setattr(agent, "run_scan_pipeline", _spy)

    reader = ReplayReader(
        record_dir=rec, date="2026-05-30", agent=agent,
        sources=("deribit", "polymarket"), jump_to_expiry=False,
    )
    await reader.run()

    # Cadence, not terminal: many scans fired (one per 5 s boundary + trailing),
    # never the single scan the old reader ran.
    assert len(calls) > 50
    # Every boundary scan fired at a clean 5 s sim-offset off the first frame.
    for scan_now, _ in calls[:-1]:
        offset = (scan_now - t0).total_seconds()
        assert abs(offset % 5.0) < 1e-6, f"scan at non-boundary offset {offset}"
    # The early PM tick (t0+1 s) is SCANNED FRESH: it first appears in a scan
    # whose sim-time is within one interval of arrival -> pm_age <= 5 s, far
    # under the 300 s cutoff that staled it under the terminal scan.
    pm_ts = t0 + _td(seconds=1)
    fresh = [(s - pm_ts).total_seconds() for s, tss in calls if pm_ts in tss]
    assert fresh, "early PM tick was never scanned"
    assert min(fresh) <= _SCAN_INTERVAL.total_seconds() + 1e-6
    # Contrast: the window exceeds the 300 s gate, so a single terminal scan
    # (the OLD behaviour) WOULD have staled this very tick.
    assert (calls[-1][0] - pm_ts).total_seconds() > 300.0


async def test_replay_requires_replay_clock(tmp_path, monkeypatch):
    """The reader refuses a live-mode clock -- it is the only clock driver and
    a live clock would silently ignore advance_to."""
    monkeypatch.setattr("btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path))
    agent = Agent(dry_run=True, clock=SimulatedClock("live"))
    with pytest.raises(ValueError, match="replay-mode clock"):
        ReplayReader(record_dir=tmp_path, date="2026-05-30", agent=agent)


async def test_replay_reaches_no_network(tmp_path, monkeypatch):
    """Determinism bar: replay issues ZERO HTTP requests.  Patch the httpx
    request path to explode and assert the reader still completes."""
    import httpx

    async def _boom(*_a, **_k):
        raise AssertionError("replay must not touch the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)
    rec = tmp_path / "recordings"
    _write_gz(
        rec / "deribit" / "2026-05-30" / "frames-20.jsonl.gz",
        [_deribit_frame("2026-05-30T20:00:01+00:00", "BTC-31MAY26-70000-C", 70000)],
    )
    _write_gz(
        rec / "polymarket" / "2026-05-30" / "frames-20.jsonl.gz",
        [
            _pm_markets_frame("2026-05-30T20:00:00+00:00", "TOKEN_YES"),
            _pm_book_frame("2026-05-30T20:00:03+00:00", "TOKEN_YES"),
        ],
    )
    agent = _make_replay_agent(tmp_path / "ledger", monkeypatch)
    reader = ReplayReader(record_dir=rec, date="2026-05-30", agent=agent)
    stats = await reader.run()   # must not raise
    assert stats["frames"] == 3


# ── Cold-path smoke against the REAL recordings (debt retirement) ─────────────


def _round_floats(obj, ndigits: int = 6):
    """Recursively round floats so the comparison ignores sub-microscopic
    BLAS-reduction FP noise in the surface-fit-derived edge columns.

    The SVI smile fit reduces over option strikes via numpy/BLAS, whose
    multi-threaded reduction order is not bit-reproducible run-to-run (~1e-7).
    That noise touches only the surface-derived float fields (raw_edge /
    adjusted_edge / fill_adjusted_edge / confidence / theoretical_edge); the
    discrete decisions (side, size, outcome, settlement_price) and the
    book-VWAP fill price + realized P&L are exact.  Rounding to 6 dp (1e-6 on
    a [0,1] probability -- far below any actionable precision) keeps the
    determinism assertion on decisions/prices/P&L without asserting bit
    identity on a number no consumer reads past 6 places.
    """
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _project(record, drop: set[str]) -> dict:
    """Semantic projection of a ledger record -- volatile stamp/identity
    columns dropped (determinism decision (b)), surface-fit floats rounded."""
    dumped = {
        k: v for k, v in record.model_dump(mode="json").items() if k not in drop
    }
    return _round_floats(dumped)


def _project_ledger(ledger_dir: Path) -> dict:
    from btc_pm_arb.execution.paper_ledger import PaperLedger

    led = PaperLedger(ledger_dir)
    order_drop = {"client_order_id", "created_at", "run_id"}
    intent_drop = {"client_order_id", "created_at", "run_id"}
    fill_drop = {"client_order_id", "filled_at", "run_id"}
    settle_drop = {"client_order_id", "settled_at", "run_id"}
    rej_drop = {"timestamp", "run_id"}
    return {
        "orders": [_project(r, order_drop) for r in led.replay_orders()],
        "intents": [_project(r, intent_drop) for r in led.replay_intents()],
        "fills": [_project(r, fill_drop) for r in led.replay_fills()],
        "settlements": [_project(r, settle_drop) for r in led.replay_settlements()],
        "rejections": [_project(r, rej_drop) for r in led.replay_rejections()],
    }


async def _replay_real(tmp_path: Path, monkeypatch, *, jump: bool = True) -> Agent:
    """One cold replay of the real window-20 capture into ``tmp_path``."""
    agent = _make_replay_agent(tmp_path, monkeypatch)
    reader = ReplayReader(
        record_dir=_REC_DIR, date=_REC_DATE, agent=agent,
        sources=("deribit", "polymarket"), hours=("20",), jump_to_expiry=jump,
    )
    await reader.run()
    return agent


@_needs_recordings
async def test_cold_path_replay_reproduces_full_chain(tmp_path, monkeypatch):
    """From an EMPTY surface (no seeded cache), replay reproduces
    signal -> fill -> ledger -> settlement against the real recordings."""
    agent = await _replay_real(tmp_path / "run", monkeypatch, jump=True)

    orders = list(agent.paper_ledger.replay_orders())
    intents = list(agent.paper_ledger.replay_intents())
    fills = list(agent.paper_ledger.replay_fills())
    settlements = list(agent.paper_ledger.replay_settlements())

    assert len(orders) >= 1, "cold replay produced no order intent"
    # Build step 6: a shadow no-op intent rides with every order, submitting
    # nothing (criterion 2 reproduced under replay).
    assert len(intents) == len(orders)
    assert all(i.submitted is False for i in intents)
    assert {i.client_order_id for i in intents} == {o.client_order_id for o in orders}
    assert any(f.fill_outcome in ("full", "partial") for f in fills)
    assert len(settlements) >= 1, "jump-to-expiry produced no settlement"
    # All settled positions are Polymarket benchmark settlements (PM is the
    # active paper venue; the price source is the deterministic model).
    assert all(s.platform.value == "polymarket" for s in settlements)
    # Every ledger append is stamped replay-mode (build step 4 ride-through).
    assert all(o.mode == "replay" for o in orders)


@_needs_recordings
async def test_two_cold_replays_are_identical(tmp_path, monkeypatch):
    """THE seeded-cache-debt retirement: two cold replays of one recording
    yield identical ledgers on the semantic projection (decision (b))."""
    await _replay_real(tmp_path / "a", monkeypatch, jump=True)
    await _replay_real(tmp_path / "b", monkeypatch, jump=True)

    proj_a = _project_ledger(tmp_path / "a")
    proj_b = _project_ledger(tmp_path / "b")

    # Non-trivial: the chain actually fired, so identity is meaningful.
    assert proj_a["orders"] and proj_a["intents"] and proj_a["fills"]
    assert proj_a["settlements"]
    for stream in ("orders", "intents", "fills", "settlements", "rejections"):
        assert proj_a[stream] == proj_b[stream], f"{stream} differ across replays"


@_needs_recordings
async def test_jump_to_expiry_gates_settlement(tmp_path, monkeypatch):
    """Without the jump-to-expiry fast-forward the sim-clock never reaches a
    contract expiry (plan 1.4), so the benchmark settler does NOT fire even
    though positions are open; WITH the jump it settles deterministically."""
    no_jump = await _replay_real(tmp_path / "nojump", monkeypatch, jump=False)
    assert len(no_jump.paper_positions.open_positions()) >= 1
    assert list(no_jump.paper_ledger.replay_settlements()) == []

    jumped = await _replay_real(tmp_path / "jump", monkeypatch, jump=True)
    assert len(list(jumped.paper_ledger.replay_settlements())) >= 1


# ── C2: empty/zero-frame replay guard ─────────────────────────────────────────

async def test_run_no_recordings_for_date_exits_clean_with_open_positions(
    tmp_path, monkeypatch,
):
    """A no-data replay date must NOT crash on the unanchored clock.

    Regression: with open positions present (replayed from disk at Agent
    init), run() reaches _jump_to_expiry_and_settle, which reads
    agent.clock.now() unconditionally (replay.py -> clock.py:82).  When the
    day dir is empty the clock was never positioned, so that read raised
    RuntimeError.  The frames==0 guard exits cleanly instead.
    """
    from types import SimpleNamespace

    monkeypatch.setattr("btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path))
    # Unanchored replay clock, exactly as main.run() constructs it.
    agent = Agent(dry_run=True, clock=SimulatedClock("replay"), run_id="testrun")

    # Force the open-positions path so the (previously crashing) jump-to-expiry
    # branch would be reached but for the guard.
    fake_pos = SimpleNamespace(expiry=datetime(2026, 6, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(
        agent.paper_positions, "open_positions", lambda: [fake_pos],
    )

    # tmp_path has no recordings -> zero frames for any date.
    reader = ReplayReader(record_dir=tmp_path, date="2026-06-01", agent=agent)
    stats = await reader.run()  # must NOT raise

    assert stats["frames"] == 0
    assert stats["settlements"] == 0
    # We exited before touching the clock: it is still unpositioned.
    with pytest.raises(RuntimeError):
        agent.clock.now()
