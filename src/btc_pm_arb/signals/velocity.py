"""Odds velocity tracker — measures the rate of PM price change per contract.

An *odds velocity* is the time-derivative of the YES probability:

    v(t) = ΔP / Δt   [probability per second]

Interpretation
--------------
* |v| large AND price CONVERGING toward implied probability:
    → the market is already correcting; don't chase a closing edge.
* |v| large AND price DIVERGING from implied probability:
    → the edge is widening; confidence boost warranted.
* |v| small: price is stable; no velocity-based adjustment.

The tracker maintains a rolling deque of (timestamp, yes_price) observations
per contract and exposes ``velocity_at(contract_id, implied_prob)`` which
returns the velocity and whether it is converging or diverging.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import numpy as np
import structlog

logger: structlog.BoundLogger = structlog.get_logger(__name__)

Direction = Literal["converging", "diverging", "stable"]


@dataclass
class VelocityResult:
    velocity: float           # ΔP/Δt in prob/second (signed)
    direction: Direction
    n_samples: int            # observations used
    window_secs: float        # actual window covered


class OddsVelocityTracker:
    """Track and query PM price velocity per contract.

    Args:
        window_secs:       Rolling window for velocity estimation (default 30 s).
        min_velocity:      Threshold below which velocity is considered zero / stable.
        max_history:       Max stored observations per contract.

    Usage::

        tracker = OddsVelocityTracker()
        tracker.update("btc-100k", yes_price=0.43)
        result = tracker.velocity_at("btc-100k", implied_prob=0.57)
        if result.direction == "converging" and abs(result.velocity) > 0.01:
            # signal rejected — market closing the gap
    """

    def __init__(
        self,
        window_secs: float = 30.0,
        min_velocity: float = 5e-4,     # 0.05 % / second
        max_history: int = 500,
    ) -> None:
        self.window_secs = window_secs
        self.min_velocity = min_velocity
        self._history: dict[str, deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=max_history)
        )

    def update(
        self,
        contract_id: str,
        yes_price: float,
        ts: datetime | None = None,
    ) -> None:
        """Record a new YES price observation."""
        now = ts or datetime.now(timezone.utc)
        self._history[contract_id].append((now, yes_price))

    def velocity_at(
        self,
        contract_id: str,
        implied_prob: float,
        ts: datetime | None = None,
    ) -> VelocityResult | None:
        """Compute velocity and direction for a contract.

        Args:
            contract_id:  Contract identifier.
            implied_prob: Options-implied probability (used to determine direction).
            ts:           Reference timestamp (defaults to now).

        Returns:
            VelocityResult, or None if insufficient history.
        """
        now = ts or datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.window_secs)
        buf = self._history.get(contract_id)
        if buf is None:
            return None

        pts = [(t, p) for t, p in buf if t >= cutoff]
        if len(pts) < 2:
            return None

        times = np.array([(t - pts[0][0]).total_seconds() for t, _ in pts])
        prices = np.array([p for _, p in pts])

        # Linear regression slope as velocity estimate (robust vs endpoint-only)
        if times[-1] <= 0:
            return None
        slope = float(np.polyfit(times, prices, 1)[0])   # prob/second

        window_covered = (pts[-1][0] - pts[0][0]).total_seconds()

        # Direction: is the PM price moving toward or away from implied_prob?
        current_price = prices[-1]
        gap_before = implied_prob - prices[0]
        gap_after = implied_prob - current_price

        if abs(slope) < self.min_velocity:
            direction: Direction = "stable"
        elif abs(gap_after) < abs(gap_before):
            direction = "converging"
        else:
            direction = "diverging"

        return VelocityResult(
            velocity=slope,
            direction=direction,
            n_samples=len(pts),
            window_secs=window_covered,
        )

    def history(self, contract_id: str) -> list[tuple[datetime, float]]:
        """Return raw history for a contract."""
        return list(self._history.get(contract_id, []))

    def clear(self, contract_id: str | None = None) -> None:
        if contract_id is None:
            self._history.clear()
        else:
            self._history.pop(contract_id, None)
