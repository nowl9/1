"""Deterministic benchmark settlement for Polymarket paper positions.

Build step 3 (paper-trading plan sections 3.3, 4.2; Fork 2).

Kalshi paper positions settle against Kalshi's own live oracle (the
``KalshiSettlementPoller`` polls GET /markets/{ticker}).  Polymarket paper
positions instead settle against a DETERMINISTIC benchmark MODEL evaluated
at expiry -- NOT against Polymarket's own resolution oracle.

Why a deterministic model (Fork 2)
----------------------------------
Determinism is what makes Criterion 6 possible: a replay (build step 5 --
SEPARATE follow-up) must reproduce the settlement price from the recorded
surface alone, with no live oracle call.  A terminal-digital benchmark
model evaluated at the expiry instant does exactly that -- given the same
benchmark BTC fixing, strike, and direction, it returns the same
settlement price every time.

Known gap (Carried Gap a)
-------------------------
Polymarket actually resolves via its own oracle, not a CF-Benchmarks-RTI
index.  The benchmark is a deterministic MODEL of the settlement price, so
paper settlements can differ from the real oracle outcome by a basis-like
amount.  Accepted for v1; quantify in P&L analysis and flip Fork 2 if the
wedge proves material.

Reuse
-----
The settlement-record shape (:class:`PaperSettlementRecord`) and the
position-close mechanics are shared with the Kalshi path via
:func:`paper_settlement.build_settlement_record`; only the price SOURCE
(this benchmark model) differs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Literal

import structlog

from btc_pm_arb.execution.paper_ledger import PaperLedger, PaperOrderRecord
from btc_pm_arb.execution.paper_positions import PaperPositionTracker
from btc_pm_arb.execution.paper_settlement import _default_clock, build_settlement_record
from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# -- Benchmark model ---------------------------------------------------------


def benchmark_settlement_price(
    benchmark_price: float,
    strike: float,
    direction: Literal["above", "below"],
) -> float:
    """Deterministic CF-Benchmarks-RTI-style terminal-digital settlement.

    Returns the YES settlement price in {0.0, 1.0}: the probability the
    contract resolves YES collapses to a certainty once the benchmark BTC
    fixing at expiry is known.

      - direction "above": YES wins (1.0) iff ``benchmark_price >= strike``.
      - direction "below": YES wins (1.0) iff ``benchmark_price <  strike``.

    Pure and total -- no I/O, no clock, no randomness -- so a replay
    reproduces it bit-for-bit from the recorded surface (Fork 2).
    """
    if direction == "below":
        return 1.0 if benchmark_price < strike else 0.0
    return 1.0 if benchmark_price >= strike else 0.0


# -- Settler -----------------------------------------------------------------


class PaperBenchmarkSettler:
    """Settle expired Polymarket paper positions against the benchmark model.

    The deterministic counterpart to :class:`KalshiSettlementPoller`: no
    HTTP, no live oracle.  At/after a position's expiry (read through the
    injected sim-clock seam), it obtains the benchmark BTC fixing from
    ``benchmark_price_fn`` (which, in replay, reads the recorded surface),
    evaluates :func:`benchmark_settlement_price`, and closes the position
    via the shared :func:`build_settlement_record` mechanics.

    PM-only by construction: Kalshi positions keep settling through their
    live-oracle poller, so this settler skips them.

    Usage::

        settler = PaperBenchmarkSettler(
            tracker=paper_positions,
            ledger=paper_ledger,
            get_order_record=lambda cid: orders_by_id.get(cid),
            benchmark_price_fn=lambda expiry: latest_btc_index_or_none,
            clock=sim_clock,
        )
        settler.settle_due()    # idempotent: closed positions are skipped
    """

    def __init__(
        self,
        *,
        tracker: PaperPositionTracker,
        ledger: PaperLedger,
        get_order_record: Callable[[str], PaperOrderRecord | None],
        benchmark_price_fn: Callable[[datetime], float | None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tracker = tracker
        self._ledger = ledger
        self._get_order_record = get_order_record
        self._benchmark_price_fn = benchmark_price_fn
        self._clock = clock or _default_clock

    def settle_due(self) -> int:
        """Settle every open PM position whose expiry has passed.

        Returns the number of settlements recorded.  Skips (leaves open):
        non-PM positions; positions not yet expired; positions whose
        originating order lacks a strike; and positions for which the
        benchmark fixing is unavailable -- each logged, never settled with
        a guess (mirrors the Kalshi poller's fail-safe-open posture).
        """
        now = self._clock()
        settled = 0
        for pos in self._tracker.open_positions():
            if pos.platform != DataSource.POLYMARKET:
                continue
            if pos.expiry > now:
                continue

            client_order_id = pos.order_ids[0] if pos.order_ids else ""
            order = self._get_order_record(client_order_id) if client_order_id else None
            if order is None or order.strike is None:
                logger.warning(
                    "benchmark_settlement.missing_strike",
                    contract=pos.contract_id,
                    client_order_id=client_order_id,
                )
                continue

            benchmark_price = self._benchmark_price_fn(pos.expiry)
            if benchmark_price is None:
                logger.warning(
                    "benchmark_settlement.no_benchmark_price",
                    contract=pos.contract_id,
                    expiry=pos.expiry.isoformat(),
                )
                continue

            settlement_price = benchmark_settlement_price(
                benchmark_price, order.strike, order.direction,
            )
            record = build_settlement_record(
                pos=pos,
                settlement_price=settlement_price,
                now=now,
                theoretical_edge=order.adjusted_edge,
            )
            self._ledger.append_settlement(record)
            self._tracker.settle(record)
            settled += 1

            logger.info(
                "benchmark_settlement.recorded",
                contract=pos.contract_id,
                platform=pos.platform.value,
                side=pos.side,
                outcome=record.outcome,
                benchmark_price=round(benchmark_price, 2),
                strike=order.strike,
                direction=order.direction,
                settlement_price=settlement_price,
                realized_pnl=round(record.realized_pnl, 4),
                theoretical_edge=round(order.adjusted_edge, 4),
            )
        return settled
