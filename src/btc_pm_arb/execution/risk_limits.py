"""First-class risk-limit layer -- declarative caps, distinct from edge gates.

The edge/confidence gates (signals/filters.py + the confidence backfill in
main.run_scan_pipeline) decide whether a contract is MISPRICED.  This module
decides whether we are ALLOWED to add exposure given current portfolio
state -- a separate question the edge gates never ask.  It is evaluated in
``Agent.run_scan_pipeline`` strictly AFTER every edge/confidence gate and
strictly BEFORE ``OrderManager.place`` builds an order (see
docs/diag_risklimits.md for the Phase 1 trace of why pre-place blocking is
load-bearing: place() registers the dedupe fingerprint, so a post-place
block would suppress the signal forever even after headroom returns).

Three declarative caps (reference shape: ImMike/polymarket-arbitrage):

  - ``max_position_per_market``  -- open USD on one (platform, contract)
  - ``max_global_exposure``      -- open USD across all positions
  - ``max_daily_loss``           -- realized loss for the current UTC day
                                    (positive magnitude; blocks new orders
                                    once daily realized P&L <= -cap)

PAPER values only.  ``max_daily_loss`` is REALIZED-only by design: marks
are never persisted (PaperPosition.current_mid is in-memory), so an
unrealized variant would require new persistence -- out of scope.

run_id scoping -- why state is read back from the ledger files
--------------------------------------------------------------
Caps are evaluated against run_id-scoped event-sourced state ONLY.  The
agent's in-memory ``PaperPositionTracker`` is rehydrated at startup from
ALL records in the shared ledger dir with no run_id filter (main.py
startup replay), so it is cross-run contaminated -- a prior misread saw
137 None-tagged records inflate a position/exposure count.  This module
therefore reads the JSONL streams directly and filters every record by
``record.run_id == run_id``:

  - every persisted line is run_id-stamped at append time
    (``PaperLedger._append``) and fsynced before the append returns, so a
    same-process read at order time sees every current-run record;
  - the current run_id is a uuid4 hex, never ``""``, so the equality
    filter excludes both other-run records and legacy/None-tagged records
    (which deserialize to the ``run_id=""`` field default) in one
    predicate;
  - reading from disk sidesteps the in-memory stamping asymmetry entirely
    (the stamp lands on the serialized copy; callers' in-memory records
    keep ``run_id=""``, so they must never be filtered by that field).

EXPLICIT CONSEQUENCE of run_id scoping (mandated by the goal, not an
accident): caps measure THIS RUN's activity.  A process restart mints a
fresh run_id, so the cap window restarts with it -- prior-run open
positions (which the in-memory tracker still carries and the settlers
still manage) do not count toward the exposure caps, and a daily-loss
amount realized earlier the same day under a previous run_id does not
count toward the brake.  That is the price of making cross-run ledger
accumulation unable to contaminate a cap; a cross-run cap (e.g. filter
``run_id != ""``) is a deliberate future policy change, not a bug fix.

Per-intent read cost: the builder runs only for intents the OrderManager
would not dedupe (at most one per unique signal fingerprint per
breach-tick), but each run parses the FULL orders/fills/settlements
files before the run_id filter applies, and the shared ledger dir
accumulates across runs -- O(lifetime history), not O(current run).
Acceptable at paper cadence (the project already accepts inline
per-append fsync on this same code path as not latency-critical,
paper_ledger.py module docstring); revisit with an offset cache if the
ledger dir ever accumulates months of multi-run records.

Configuration -- pydantic-settings, OS env wins over .env
---------------------------------------------------------
:class:`RiskLimits` follows the repo convention (config.Settings):
``BaseSettings`` with ``env_file=".env"``; pydantic-settings source
priority makes OS environment variables win over .env values, which win
over the code defaults below.  Fields use the ``risk_`` env prefix
(``RISK_MAX_POSITION_PER_MARKET``, ``RISK_MAX_GLOBAL_EXPOSURE``,
``RISK_MAX_DAILY_LOSS``) so nothing in the existing live .env can
collide.  The live .env is NOT modified; the defaults below are the
PAPER values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timezone
from typing import Literal

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from btc_pm_arb.execution.paper_ledger import PaperLedger
from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# -- Configuration -------------------------------------------------------------


class RiskLimits(BaseSettings):
    """Declarative risk caps -- PAPER defaults, env-overridable.

    Defaults are sized against the fixed paper order size
    (``main._BASE_SIZE_USD`` = 200): 500 per market allows two full base
    orders plus a partial on one market; 5000 global matches the
    precedent set by the retired execution/risk.py RiskConfig; 500
    daily loss stops a paper run after losing 2.5 base orders realized
    in one UTC day.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="risk_",
    )

    # Open USD cap on a single (platform, contract_id) market.  Both sides
    # (yes and no) of the market count toward the cap: a hedged yes+no
    # structure is still capital at risk on that market.
    max_position_per_market: float = Field(default=500.0, gt=0)
    # Open USD cap across ALL open paper positions in the current run.
    max_global_exposure: float = Field(default=5000.0, gt=0)
    # Positive magnitude.  Once the current run's realized P&L for the
    # current UTC day is <= -max_daily_loss, every new intent is blocked
    # for the rest of that day.  Realized-only (settlements); marks are
    # not persisted, so unrealized losses do not count.
    max_daily_loss: float = Field(default=500.0, gt=0)


# -- Inputs to the pure check --------------------------------------------------


@dataclass(frozen=True)
class RiskIntent:
    """The order intent under evaluation -- known before any Order is built."""

    platform: DataSource
    contract_id: str
    side: Literal["yes", "no"]
    size_usd: float


@dataclass(frozen=True)
class PortfolioState:
    """run_id-scoped portfolio numbers the caps are evaluated against.

    Produced by :func:`build_portfolio_state`; consumed by
    :func:`check_risk`.  Carrying the three numbers (rather than a live
    tracker reference) keeps ``check_risk`` pure and trivially testable.
    """

    # Open USD on the intent's (platform, contract_id), both sides.
    market_position_usd: float
    # Open USD across all of the current run's open positions.
    global_exposure_usd: float
    # Realized P&L from the current run's settlements dated today.
    daily_realized_pnl_usd: float


# -- Pure check ----------------------------------------------------------------


def check_risk(
    intent: RiskIntent,
    state: PortfolioState,
    limits: RiskLimits,
) -> tuple[bool, str]:
    """Evaluate the three caps; return ``(allow, reason)``.

    Pure: no I/O, no logging side effects on the decision, never raises.
    Caps are evaluated in declaration order (per-market, global,
    daily-loss); the first breach wins and its reason is returned.
    Exactly-at-cap is allowed for the exposure caps (blocking is on
    strict ``>``); the daily-loss brake trips on ``<=`` of the negative
    cap (losing exactly the cap amount trips it).
    """
    if state.market_position_usd + intent.size_usd > limits.max_position_per_market:
        return False, (
            f"market_position {state.market_position_usd:.2f} + intent "
            f"{intent.size_usd:.2f} > max_position_per_market "
            f"{limits.max_position_per_market:.2f} "
            f"({intent.platform.value}:{intent.contract_id})"
        )
    if state.global_exposure_usd + intent.size_usd > limits.max_global_exposure:
        return False, (
            f"global_exposure {state.global_exposure_usd:.2f} + intent "
            f"{intent.size_usd:.2f} > max_global_exposure "
            f"{limits.max_global_exposure:.2f}"
        )
    if state.daily_realized_pnl_usd <= -limits.max_daily_loss:
        return False, (
            f"daily_realized_pnl {state.daily_realized_pnl_usd:.2f} <= "
            f"-max_daily_loss -{limits.max_daily_loss:.2f}"
        )
    return True, ""


# -- run_id-scoped state builder -----------------------------------------------


def build_portfolio_state(
    ledger: PaperLedger,
    *,
    run_id: str,
    platform: DataSource,
    contract_id: str,
    today: date,
) -> PortfolioState:
    """Reconstruct the three cap inputs from the event-sourced JSONL streams.

    Reads orders/fills/settlements through the given ``ledger`` (a
    read-side instance -- the caller passes a dedicated reader so the
    main ledger's load-verification counters stay meaningful) and keeps
    ONLY records with ``record.run_id == run_id``.

    Accumulation mirrors ``PaperPositionTracker`` semantics exactly:

      - fills join to their order on ``client_order_id``; ``no_fill``,
        ``fill_price is None``, and non-positive ``fill_size_usd`` are
        skipped (paper_positions.record_fill's skip rules);
      - position size per (platform, contract_id, side) triple is the
        sum of ``fill_size_usd`` (paper_positions accumulation);
      - a settlement closes its (platform, contract_id, side) triple
        (paper_positions.settle keying), removing it from exposure;
      - daily realized P&L sums ``realized_pnl`` over settlements whose
        ``settled_at`` falls on ``today`` (UTC).  ``today`` must come
        from the agent's sim-clock seam so replay buckets by sim-time.

    A current-run settlement for a triple opened in a PRIOR run finds no
    current-run fills to close (harmless no-op for exposure) but DOES
    count toward today's realized P&L: within the run-scoped window the
    brake cares about money THIS RUN realized today, regardless of which
    run opened the position.  The converse does not hold -- settlements
    written by an earlier process today carry that run's id and are
    excluded (see the module docstring's run-scoping consequence note).
    """
    orders_by_id: dict[str, tuple[DataSource, str, str]] = {}
    for order in ledger.replay_orders():
        if order.run_id != run_id:
            continue
        orders_by_id[order.client_order_id] = (
            order.platform, order.contract_id, order.side,
        )

    filled_by_triple: dict[tuple[DataSource, str, str], float] = {}
    for fill in ledger.replay_fills():
        if fill.run_id != run_id:
            continue
        triple = orders_by_id.get(fill.client_order_id)
        if triple is None:
            # Fill whose order is missing or out-of-run: nothing to
            # attribute (mirrors the rehydration path's orphan-fill skip).
            continue
        if fill.fill_outcome == "no_fill":
            continue
        if fill.fill_price is None or fill.fill_size_usd <= 0:
            continue
        filled_by_triple[triple] = (
            filled_by_triple.get(triple, 0.0) + fill.fill_size_usd
        )

    closed_triples: set[tuple[DataSource, str, str]] = set()
    daily_realized_pnl = 0.0
    for settlement in ledger.replay_settlements():
        if settlement.run_id != run_id:
            continue
        closed_triples.add(
            (settlement.platform, settlement.contract_id, settlement.side)
        )
        settled_at = settlement.settled_at
        if settled_at.tzinfo is None:
            settled_at = settled_at.replace(tzinfo=timezone.utc)
        if settled_at.astimezone(timezone.utc).date() == today:
            daily_realized_pnl += settlement.realized_pnl

    market_position = 0.0
    global_exposure = 0.0
    for triple, size in filled_by_triple.items():
        if triple in closed_triples:
            continue
        global_exposure += size
        if triple[0] == platform and triple[1] == contract_id:
            market_position += size

    return PortfolioState(
        market_position_usd=market_position,
        global_exposure_usd=global_exposure,
        daily_realized_pnl_usd=daily_realized_pnl,
    )
