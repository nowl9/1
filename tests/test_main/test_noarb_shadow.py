"""Pipeline tests for the no-arb SHADOW layer (no-arb goal Phase 3).

Drives the REAL wiring end-to-end: hand-built smiles are planted on the
agent's surface, ``update_cache_from_surface`` runs the no-arb check at
the digital pricing site (setting/clearing ``_noarb_flags`` in lockstep
with the cache writes), and ``run_scan_pipeline`` appends one
noarb_shadow record per flagged edge.

The invariants under test are the goal's success criteria:

* a CLEAN surface logs NO shadow record and the signal emits;
* a butterfly-violating smile (Axel Vogt's classic SVI parameters) and
  a calendar-violating pair each log EXACTLY ONE record with the right
  reason -- and the signal STILL emits (shadow mode suppresses nothing);
* a violating fit replaced by a clean refit clears the flag (latest-fit
  semantics): later scans log nothing new.

Fixture builders are local copies of the test_scan_pipeline.py minimal
builders (cache-seeding there, surface-seeding here).  ``fit_rmse`` is
set on hand-built smiles because the vol_rmse filter criterion
(filters.py, max_vol_fit_rmse) reads it -- the default inf would reject
the signal and the "still emits" half of the assertion would be vacuous.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.main import Agent
from btc_pm_arb.models import (
    DataSource,
    OptionTick,
    OptionType,
    PredictionMarketTick,
)
from btc_pm_arb.pricing.vol_surface import SVIParams, VolSmile

_F = 100_000.0

# Butterfly-arbitrageable smile (g(0.9) ~ -0.0327) -- see test_noarb.py.
_VOGT = SVIParams(a=-0.0410, b=0.1331, rho=0.3060, m=0.3586, nu=0.4153)
_K_BAD = _F * math.exp(0.9)

_FLAT_LO = SVIParams(a=0.01, b=1e-3, rho=0.0, m=0.0, nu=0.1)
_FLAT_HI = SVIParams(a=0.02, b=1e-3, rho=0.0, m=0.0, nu=0.1)


def _make_smile(expiry: datetime, T: float, params: SVIParams) -> VolSmile:
    smile = VolSmile(expiry=expiry, forward=_F, time_to_expiry=T)
    smile.params = params
    smile.fit_rmse = 0.001
    return smile


def _make_opt_tick(name: str, strike: float, expiry: datetime) -> OptionTick:
    """Minimal tick so update_cache_from_surface finds the strike to price."""
    return OptionTick(
        instrument_name=name,
        strike=strike,
        expiry=expiry,
        option_type=OptionType.CALL,
        bid=0.01,
        ask=0.02,
        mark_price=0.015,
        mark_iv=50.0,
        underlying_price=_F,
        index_price=_F,
        timestamp=datetime.now(timezone.utc),
    )


def _make_pm_tick(
    contract_id: str,
    strike: float,
    expiry: datetime,
    *,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    no_ask: float,
) -> PredictionMarketTick:
    return PredictionMarketTick(
        source=DataSource.KALSHI,
        contract_id=contract_id,
        question="BTC above strike?",
        strike=strike,
        expiry=expiry,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        order_book_yes=[(yes_ask, 500.0)],
        order_book_no=[(no_ask, 500.0)],
        timestamp=datetime.now(timezone.utc),
    )


def _make_agent(monkeypatch, tmp_path) -> Agent:
    monkeypatch.setattr(
        "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
    )
    agent = Agent(dry_run=True)
    # Seed feed-health so the freshness gate doesn't reject (same as the
    # reference end-to-end test in test_scan_pipeline.py).
    agent.feed_health.record_tick(DataSource.DERIBIT)
    agent.feed_health.record_tick(DataSource.KALSHI)
    return agent


def _plant_and_price(agent: Agent, smiles: dict, ticks: dict) -> None:
    agent.surface._smiles.update(smiles)
    agent.surface._ticks.update(ticks)
    agent.update_cache_from_surface(set(smiles))


class TestNoarbShadowPipeline:
    @pytest.mark.asyncio
    async def test_clean_surface_logs_nothing_and_signal_emits(
        self, monkeypatch, tmp_path,
    ) -> None:
        agent = _make_agent(monkeypatch, tmp_path)
        e1 = datetime.now(timezone.utc) + timedelta(days=3, hours=12)
        e2 = datetime.now(timezone.utc) + timedelta(days=7)
        _plant_and_price(
            agent,
            {
                e1: _make_smile(e1, 3.5 / 365.25, _FLAT_LO),
                e2: _make_smile(e2, 7 / 365.25, _FLAT_HI),
            },
            {
                "T1": _make_opt_tick("T1", _F, e1),
                "T2": _make_opt_tick("T2", _F, e2),
            },
        )
        assert agent._noarb_flags == {}

        agent.ingest_pm_tick(_make_pm_tick(
            "CLEAN-1", _F, e2, yes_bid=0.28, yes_ask=0.30, no_bid=0.70, no_ask=0.72,
        ))
        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        assert list(agent.paper_ledger.replay_noarb_shadow()) == []
        assert agent._funnel["noarb_shadow_flagged"] == 0
        # The signal emitted normally.
        assert agent._funnel["signals_passed_filter"] == 1

    @pytest.mark.asyncio
    async def test_butterfly_violation_logs_one_record_and_signal_still_emits(
        self, monkeypatch, tmp_path,
    ) -> None:
        agent = _make_agent(monkeypatch, tmp_path)
        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        _plant_and_price(
            agent,
            {expiry: _make_smile(expiry, 7 / 365.25, _VOGT)},
            {"T1": _make_opt_tick("T1", _K_BAD, expiry)},
        )
        # The pricing site flagged the grid point.
        assert (_K_BAD, expiry) in agent._noarb_flags

        agent.ingest_pm_tick(_make_pm_tick(
            "BFLY-1", _K_BAD, expiry,
            yes_bid=0.30, yes_ask=0.32, no_bid=0.60, no_ask=0.62,
        ))
        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        records = list(agent.paper_ledger.replay_noarb_shadow())
        assert len(records) == 1
        rec = records[0]
        assert rec.would_reject is True
        assert len(rec.reasons) == 1
        assert rec.reasons[0].startswith("butterfly:g=-")
        assert rec.contract_id == "BFLY-1"
        assert rec.platform == DataSource.KALSHI
        assert rec.strike == pytest.approx(_K_BAD)
        assert rec.expiry == expiry
        assert rec.best_side == "buy_no"
        assert rec.best_conservative_edge > 0
        # SHADOW ONLY: the artifact edge STILL fired as a signal.
        assert rec.signal_emitted is True
        assert agent._funnel["signals_passed_filter"] == 1
        assert agent._funnel["noarb_shadow_flagged"] == 1

    @pytest.mark.asyncio
    async def test_calendar_violation_logs_one_record_and_signal_still_emits(
        self, monkeypatch, tmp_path,
    ) -> None:
        agent = _make_agent(monkeypatch, tmp_path)
        e1 = datetime.now(timezone.utc) + timedelta(days=3, hours=12)
        e2 = datetime.now(timezone.utc) + timedelta(days=7)
        # Longer slice has LOWER total variance: crossed calendar.
        _plant_and_price(
            agent,
            {
                e1: _make_smile(e1, 3.5 / 365.25, _FLAT_HI),
                e2: _make_smile(e2, 7 / 365.25, _FLAT_LO),
            },
            {
                "T1": _make_opt_tick("T1", _F, e1),
                "T2": _make_opt_tick("T2", _F, e2),
            },
        )
        # Both grid points flagged: the crossing seen from each side.
        assert agent._noarb_flags[(_F, e2)][0].startswith("calendar_vs_prev:")
        assert agent._noarb_flags[(_F, e1)][0].startswith("calendar_vs_next:")

        agent.ingest_pm_tick(_make_pm_tick(
            "CAL-1", _F, e2, yes_bid=0.28, yes_ask=0.30, no_bid=0.70, no_ask=0.72,
        ))
        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        records = list(agent.paper_ledger.replay_noarb_shadow())
        assert len(records) == 1
        rec = records[0]
        assert rec.would_reject is True
        assert rec.reasons == agent._noarb_flags[(_F, e2)]
        assert rec.reasons[0].startswith("calendar_vs_prev:w=")
        assert rec.contract_id == "CAL-1"
        assert rec.strike == _F
        assert rec.expiry == e2
        assert rec.best_side == "buy_yes"
        assert rec.best_conservative_edge > 0
        assert rec.signal_emitted is True
        assert agent._funnel["signals_passed_filter"] == 1
        assert agent._funnel["noarb_shadow_flagged"] == 1

    @pytest.mark.asyncio
    async def test_clean_refit_clears_flag_so_later_scans_log_nothing(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Latest-fit semantics: the flag describes the fit that produced
        the CURRENT cache entry; a clean refit clears it."""
        agent = _make_agent(monkeypatch, tmp_path)
        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        _plant_and_price(
            agent,
            {expiry: _make_smile(expiry, 7 / 365.25, _VOGT)},
            {"T1": _make_opt_tick("T1", _K_BAD, expiry)},
        )
        agent.ingest_pm_tick(_make_pm_tick(
            "BFLY-1", _K_BAD, expiry,
            yes_bid=0.30, yes_ask=0.32, no_bid=0.60, no_ask=0.62,
        ))
        await agent.run_scan_pipeline(agent.flush_pm_ticks())
        assert len(list(agent.paper_ledger.replay_noarb_shadow())) == 1

        # Clean refit at the same expiry: flag must clear, no new record.
        agent.surface._smiles[expiry].params = _FLAT_LO
        agent.update_cache_from_surface({expiry})
        assert (_K_BAD, expiry) not in agent._noarb_flags

        agent.ingest_pm_tick(_make_pm_tick(
            "BFLY-1", _K_BAD, expiry,
            yes_bid=0.30, yes_ask=0.32, no_bid=0.60, no_ask=0.62,
        ))
        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        assert len(list(agent.paper_ledger.replay_noarb_shadow())) == 1
        assert agent._funnel["noarb_shadow_flagged"] == 1

    @pytest.mark.asyncio
    async def test_record_round_trips_with_run_stamp(
        self, monkeypatch, tmp_path,
    ) -> None:
        """The disk record carries the ledger's run_id/mode stamp and the
        full reason payload survives the JSONL round-trip."""
        monkeypatch.setattr(
            "btc_pm_arb.config.settings.paper_ledger_dir", str(tmp_path),
        )
        agent = Agent(dry_run=True, run_id="noarb-test-run")
        agent.feed_health.record_tick(DataSource.DERIBIT)
        agent.feed_health.record_tick(DataSource.KALSHI)
        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        _plant_and_price(
            agent,
            {expiry: _make_smile(expiry, 7 / 365.25, _VOGT)},
            {"T1": _make_opt_tick("T1", _K_BAD, expiry)},
        )
        agent.ingest_pm_tick(_make_pm_tick(
            "BFLY-1", _K_BAD, expiry,
            yes_bid=0.30, yes_ask=0.32, no_bid=0.60, no_ask=0.62,
        ))
        await agent.run_scan_pipeline(agent.flush_pm_ticks())

        (rec,) = list(agent.paper_ledger.replay_noarb_shadow())
        assert rec.kind == "noarb_shadow"
        assert rec.run_id == "noarb-test-run"
        assert rec.mode == "live"
        assert rec.reasons and all(isinstance(r, str) for r in rec.reasons)
