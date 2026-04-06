"""Pre-trade risk manager — runs checks BEFORE order submission.

All checks are pure functions so they can be unit-tested without side-effects.
The ``RiskManager.check()`` method runs the full chain and returns a
``RiskDecision(allow=True)`` or ``RiskDecision(allow=False, reason=...)``.

Risk criteria (all configurable via RiskConfig)
-----------------------------------------------
  1. max_position_per_contract — single contract notional cap (default $500)
  2. max_total_exposure        — sum of all open positions (default $5 000)
  3. max_open_positions        — count of live positions (default 20)
  4. max_correlated_exposure   — positions on strikes within strike_band_pct
                                 of each other (default 5 %, cap $2 000)
  5. min_confidence            — signal confidence score (default 0.40)

Each criterion is a separate function inserted into the ``_CHECKS`` list so
that new criteria can be added without touching ``RiskManager``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import structlog

from btc_pm_arb.models import ArbitrageSignal
from btc_pm_arb.execution.positions import Position, PositionTracker

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    """Configurable thresholds for the pre-trade risk checks."""

    max_position_per_contract_usd: float = 500.0
    max_total_exposure_usd: float = 5_000.0
    max_open_positions: int = 20
    max_correlated_exposure_usd: float = 2_000.0
    correlated_strike_band_pct: float = 0.05    # 5 % strike band
    min_confidence: float = 0.40


# ── Decision type ─────────────────────────────────────────────────────────────

@dataclass
class RiskDecision:
    allow: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allow


# ── Check functions ───────────────────────────────────────────────────────────

# Signature: (signal, proposed_size_usd, tracker, config) → rejection reason | None
_CheckFn = Callable[["ArbitrageSignal", float, PositionTracker, RiskConfig], str | None]


def _check_confidence(
    signal: ArbitrageSignal, size: float, tracker: PositionTracker, cfg: RiskConfig
) -> str | None:
    if signal.confidence < cfg.min_confidence:
        return f"confidence {signal.confidence:.3f} < min {cfg.min_confidence}"
    return None


def _check_position_per_contract(
    signal: ArbitrageSignal, size: float, tracker: PositionTracker, cfg: RiskConfig
) -> str | None:
    existing = tracker.get(signal.pm_quote.contract_id, signal.pm_quote.source)
    current = sum(p.notional_usd for p in existing if not p.closed)
    if current + size > cfg.max_position_per_contract_usd:
        return (
            f"position {current:.0f} + {size:.0f} > max_per_contract "
            f"{cfg.max_position_per_contract_usd:.0f}"
        )
    return None


def _check_total_exposure(
    signal: ArbitrageSignal, size: float, tracker: PositionTracker, cfg: RiskConfig
) -> str | None:
    total = tracker.total_exposure_usd()
    if total + size > cfg.max_total_exposure_usd:
        return (
            f"total_exposure {total:.0f} + {size:.0f} > max "
            f"{cfg.max_total_exposure_usd:.0f}"
        )
    return None


def _check_open_positions(
    signal: ArbitrageSignal, size: float, tracker: PositionTracker, cfg: RiskConfig
) -> str | None:
    n = len(tracker.open_positions())
    if n >= cfg.max_open_positions:
        return f"open_positions {n} >= max {cfg.max_open_positions}"
    return None


def _check_correlated_exposure(
    signal: ArbitrageSignal, size: float, tracker: PositionTracker, cfg: RiskConfig
) -> str | None:
    K = signal.pm_quote.strike
    band = cfg.correlated_strike_band_pct
    open_pos: list[Position] = tracker.open_positions()

    corr_exposure = sum(
        p.notional_usd
        for p in open_pos
        if _strike_of(p) is not None
        and abs(_strike_of(p) - K) / K <= band  # type: ignore[operator]
    )
    if corr_exposure + size > cfg.max_correlated_exposure_usd:
        return (
            f"correlated_exposure {corr_exposure:.0f} + {size:.0f} > max "
            f"{cfg.max_correlated_exposure_usd:.0f} "
            f"(band={band*100:.0f}% around K={K:.0f})"
        )
    return None


def _strike_of(pos: Position) -> float | None:
    """Best-effort: extract strike from contract_id for correlation bucketing."""
    # Contract IDs in this system include the strike as an integer suffix.
    # Falls back gracefully to None so exposure check is skipped for this pos.
    parts = pos.contract_id.split("-")
    for part in reversed(parts):
        try:
            return float(part)
        except ValueError:
            continue
    return None


_CHECKS: list[_CheckFn] = [
    _check_confidence,
    _check_position_per_contract,
    _check_total_exposure,
    _check_open_positions,
    _check_correlated_exposure,
]


# ── Risk manager ──────────────────────────────────────────────────────────────

class RiskManager:
    """Evaluate whether a proposed trade passes all pre-trade risk checks.

    Usage::

        risk = RiskManager()
        decision = risk.check(signal, proposed_size_usd=200.0, tracker=tracker)
        if decision:
            await order_mgr.place(signal, size_usd=200.0)
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def check(
        self,
        signal: ArbitrageSignal,
        proposed_size_usd: float,
        tracker: PositionTracker,
    ) -> RiskDecision:
        """Run all checks; return on first failure."""
        for check_fn in _CHECKS:
            reason = check_fn(signal, proposed_size_usd, tracker, self.config)
            if reason is not None:
                logger.info(
                    "risk.denied",
                    contract=signal.pm_quote.contract_id,
                    reason=reason,
                    proposed_size=proposed_size_usd,
                )
                return RiskDecision(allow=False, reason=reason)

        logger.debug(
            "risk.approved",
            contract=signal.pm_quote.contract_id,
            proposed_size=proposed_size_usd,
            confidence=signal.confidence,
        )
        return RiskDecision(allow=True)

    def size_for_signal(
        self,
        signal: ArbitrageSignal,
        base_size_usd: float,
        tracker: PositionTracker,
    ) -> float:
        """Compute position size adjusted for confidence and remaining headroom.

        Scales ``base_size_usd`` by signal confidence, then clips to the
        headroom remaining under the per-contract and total-exposure caps.
        """
        sized = base_size_usd * signal.confidence

        # Respect per-contract headroom
        existing = tracker.get(signal.pm_quote.contract_id, signal.pm_quote.source)
        current = sum(p.notional_usd for p in existing if not p.closed)
        per_contract_room = max(0.0, self.config.max_position_per_contract_usd - current)

        # Respect total exposure headroom
        total_room = max(0.0, self.config.max_total_exposure_usd - tracker.total_exposure_usd())

        return min(sized, per_contract_room, total_room)
