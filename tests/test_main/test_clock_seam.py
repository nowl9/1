"""Tests for the simulated-clock seam (build step 1, Fork 3).

Two things are under test:

1. :class:`btc_pm_arb.clock.SimulatedClock` behaviour -- live delegates to
   wall-clock; replay is advanceable and monotonic; it is callable.

2. The seam lands at the freshness-sensitive call sites: the
   ``filters.py`` freshness/expiry gates and ``FeedHealthTracker`` read the
   INJECTED clock, not wall-clock -- and DEFAULT (no clock) still uses real
   ``datetime.now``.  This is what makes an as-fast-as-possible replay
   (Fork 3) produce signals instead of rejecting every recorded tick as
   stale.

The CLI wiring (`--mode` / `--duration`) is checked via the testable
``_build_arg_parser``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from btc_pm_arb.clock import SimulatedClock
from btc_pm_arb.feeds.health import FeedHealthTracker
from btc_pm_arb.main import _build_arg_parser
from btc_pm_arb.models import DataSource, ProbabilityQuote, PredictionMarketTick
from btc_pm_arb.pricing.cache import CacheEntry
from btc_pm_arb.signals.edge import EdgeResult
from btc_pm_arb.signals.filters import (
    FilterConfig,
    SignalFilter,
    _reject_expiry_bounds,
    _reject_stale_data,
)
from btc_pm_arb.signals.matcher import MatchResult


# A timestamp far enough in the past that wall-clock "now" treats it as
# years stale -- the lever that distinguishes sim-time from wall-time.
_PAST = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# -- Builders ------------------------------------------------------------------


def _edge_at(
    *,
    tick_ts: datetime,
    options_ts: datetime,
    expiry: datetime,
    yes_bid: float = 0.40,
    yes_ask: float = 0.42,
) -> EdgeResult:
    """Minimal passing-shaped EdgeResult anchored at the given timestamps."""
    pm_tick = PredictionMarketTick(
        source=DataSource.KALSHI,
        contract_id="KXBTC-CLOCK-100000",
        question="BTC above $100,000?",
        strike=100_000.0,
        expiry=expiry,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=1.0 - yes_ask,
        no_ask=1.0 - yes_bid,
        order_book_yes=[(yes_ask, 500.0)],
        order_book_no=[(1.0 - yes_bid, 500.0)],
        timestamp=tick_ts,
    )
    pm_quote = ProbabilityQuote(
        source=DataSource.KALSHI,
        contract_id="KXBTC-CLOCK-100000",
        strike=100_000.0,
        expiry=expiry,
        bid_prob=yes_bid,
        ask_prob=yes_ask,
        mid_prob=(yes_bid + yes_ask) / 2,
        settlement_type="kalshi_rti",
        order_book_yes=[(yes_ask, 500.0)],
        order_book_no=[(1.0 - yes_bid, 500.0)],
        timestamp=tick_ts,
    )
    options_entry = CacheEntry(
        strike=100_000.0,
        expiry=expiry,
        bid_prob=0.55,
        ask_prob=0.55,
        mid_prob=0.55,
        source=DataSource.DERIBIT,
        timestamp=options_ts,
    )
    match = MatchResult(
        pm_tick=pm_tick,
        pm_quote=pm_quote,
        options_entry=options_entry,
        matched_strike=100_000.0,
        matched_expiry=expiry,
        strike_gap_pct=0.0,
        expiry_gap_hours=0.0,
        match_quality=1.0,
        is_interpolated=False,
    )
    return EdgeResult(
        match=match,
        edge_yes_mid=0.14,
        edge_no_mid=-0.14,
        edge_yes_conservative=0.13,
        edge_no_conservative=-0.13,
        adjusted_edge_yes=0.13,
        adjusted_edge_no=-0.13,
        best_side="buy_yes",
        best_conservative_edge=0.13,
        fill_adjusted_edge=None,
    )


# -- SimulatedClock unit behaviour ----------------------------------------------


def test_live_clock_delegates_to_wall_clock():
    clock = SimulatedClock("live")
    before = datetime.now(timezone.utc)
    got = clock.now()
    after = datetime.now(timezone.utc)
    assert before <= got <= after
    assert clock.now().tzinfo is timezone.utc


def test_clock_is_callable_drop_in_for_clock_param():
    """The object satisfies Callable[[], datetime] (poller.clock contract)."""
    clock = SimulatedClock("replay", start=_PAST)
    assert clock() == _PAST          # __call__ == now()


def test_replay_clock_returns_start_then_advances():
    clock = SimulatedClock("replay", start=_PAST)
    assert clock.now() == _PAST
    t1 = _PAST + timedelta(seconds=30)
    clock.advance_to(t1)
    assert clock.now() == t1


def test_replay_clock_is_monotonic():
    clock = SimulatedClock("replay", start=_PAST)
    clock.advance_to(_PAST + timedelta(seconds=10))
    with pytest.raises(ValueError):
        clock.advance_to(_PAST + timedelta(seconds=5))   # backwards


def test_replay_clock_raises_before_positioned():
    clock = SimulatedClock("replay")          # no start anchor
    with pytest.raises(RuntimeError):
        clock.now()


def test_advance_to_rejected_in_live_mode():
    clock = SimulatedClock("live")
    with pytest.raises(RuntimeError):
        clock.advance_to(_PAST)


def test_naive_start_coerced_to_utc():
    naive = datetime(2020, 1, 1, 0, 0, 0)     # no tzinfo
    clock = SimulatedClock("replay", start=naive)
    assert clock.now().tzinfo is timezone.utc


# -- Freshness gate reads the injected sim-clock, not wall-clock ----------------


def test_stale_gate_reads_injected_clock_not_wall_clock():
    """A tick stamped in 2020 is fresh under a sim-clock parked at 2020, but
    years-stale under wall-clock.  The gate must honour the injected clock."""
    edge = _edge_at(
        tick_ts=_PAST, options_ts=_PAST, expiry=_PAST + timedelta(days=7),
    )
    sim = SimulatedClock("replay", start=_PAST + timedelta(seconds=10))

    # Sim-clock 10 s after the tick -> fresh (well under the 300 s cutoff).
    assert _reject_stale_data(edge, FilterConfig(), {"clock": sim}) is None
    # No clock -> wall-clock (years after 2020) -> rejected as stale.
    rejection = _reject_stale_data(edge, FilterConfig(), {})
    assert rejection is not None
    assert rejection.split()[0] in {"options_data_age", "pm_data_age"}


def test_expiry_gate_reads_injected_clock_not_wall_clock():
    """Expiry 7 days after a 2020 sim-clock is in-bounds; under wall-clock
    that same expiry is years in the past and rejected."""
    expiry = _PAST + timedelta(days=7)
    edge = _edge_at(tick_ts=_PAST, options_ts=_PAST, expiry=expiry)
    sim = SimulatedClock("replay", start=_PAST)

    assert _reject_expiry_bounds(edge, FilterConfig(), {"clock": sim}) is None
    rejection = _reject_expiry_bounds(edge, FilterConfig(), {})
    assert rejection is not None
    assert rejection.startswith("days_to_expiry")


def test_default_filter_uses_wall_clock_rejects_old_tick():
    """SignalFilter.explains() with NO clock falls back to wall-clock -- a
    2020-stamped edge is rejected (default-live behaviour unchanged)."""
    edge = _edge_at(
        tick_ts=_PAST, options_ts=_PAST, expiry=_PAST + timedelta(days=7),
    )
    filt = SignalFilter(FilterConfig())
    assert filt.explains(edge) is not None       # wall-clock -> stale/expiry


def test_filter_passes_old_tick_under_injected_sim_clock():
    """SignalFilter.explains(clock=sim) threads the sim-clock into the
    freshness/expiry gates, so the same 2020-stamped edge passes."""
    edge = _edge_at(
        tick_ts=_PAST, options_ts=_PAST, expiry=_PAST + timedelta(days=7),
    )
    sim = SimulatedClock("replay", start=_PAST + timedelta(seconds=5))
    filt = SignalFilter(FilterConfig())
    assert filt.explains(edge, clock=sim) is None


# -- FeedHealthTracker reads the injected clock ---------------------------------


def test_feed_health_staleness_uses_injected_clock():
    sim = SimulatedClock("replay", start=_PAST)
    health = FeedHealthTracker(clock=sim)
    health.record_tick(DataSource.DERIBIT, ts=_PAST)      # tick at sim "now"
    # Sim-clock 2 s later -> 2000 ms staleness, regardless of wall-clock.
    sim.advance_to(_PAST + timedelta(seconds=2))
    assert health.staleness_ms(DataSource.DERIBIT) == pytest.approx(2000.0)


def test_feed_health_default_clock_is_wall_clock():
    """Default FeedHealthTracker() uses wall-clock -- a tick just recorded
    is near-zero stale (the pre-seam behaviour)."""
    health = FeedHealthTracker()
    health.record_tick(DataSource.DERIBIT)
    assert health.staleness_ms(DataSource.DERIBIT) < 1000.0


# -- CLI wiring: --mode / --duration --------------------------------------------


def test_arg_parser_defaults_to_live_unbounded():
    args = _build_arg_parser().parse_args([])
    assert args.mode == "live"
    assert args.duration is None


def test_arg_parser_accepts_replay_and_duration():
    args = _build_arg_parser().parse_args(["--mode", "replay", "--duration", "30"])
    assert args.mode == "replay"
    assert args.duration == pytest.approx(30.0)


def test_arg_parser_rejects_unknown_mode():
    with pytest.raises(SystemExit):
        _build_arg_parser().parse_args(["--mode", "backtest"])
