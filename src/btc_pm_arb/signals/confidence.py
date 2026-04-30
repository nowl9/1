"""Confidence scorer — produces a [0, 1] score for each ArbitrageSignal.

The score weights five independent quality dimensions:

  1. options_spread_tightness  — tighter options bid-ask → more reliable edge
  2. pm_depth                  — wider PM order book → less slippage risk
  3. vol_fit_quality           — low SVI RMSE → trustworthy IV surface
  4. match_quality             — close strike/expiry match → accurate pricing
  5. edge_persistence          — edge held across multiple observations → not noise

Each dimension is normalised to [0, 1] before weighting.  The final score is
a weighted average, also in [0, 1], which feeds directly into position-sizing.

Design notes
------------
* All five dimensions are independent; the scorer can run with partial
  information (missing dimensions fall back to a neutral 0.5).
* The scorer is stateless and pure — it reads from ArbitrageSignal / EdgeResult
  and optionally from a VolSurface.  No internal history is maintained; that
  responsibility belongs to EdgeCalculator.
* Adding a new dimension means adding a ``_score_*`` function and inserting it
  into ``_DIMENSIONS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import structlog

from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.signals.edge import EdgeResult

logger: structlog.BoundLogger = structlog.get_logger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class ConfidenceConfig:
    """Weights and normalisation constants for the confidence scorer."""

    # Dimension weights (will be normalised to sum to 1)
    w_options_spread: float = 1.5    # most direct measure of options mispricing risk
    w_pm_depth: float = 1.0
    w_vol_fit: float = 1.2
    w_match_quality: float = 1.0
    w_edge_persistence: float = 1.3

    # Options spread normalisation: spread of 0 → score 1, spread of max → score 0
    options_spread_max: float = 0.20     # 20 vol-pt spread → score 0

    # PM spread normalisation: tight book → high score (using bid-ask gap)
    pm_spread_max: float = 0.15          # 15 % gap → score 0

    # Vol fit normalisation: rmse 0 → 1, rmse_max → 0
    vol_rmse_max: float = 0.08           # 8 vol-pt RMSE → score 0

    # Edge persistence: persistence_max → score 1
    persistence_max: float = 1.0        # already in [0, 1] from EdgeCalculator

    # Floor/ceiling applied to the final score
    score_floor: float = 0.0
    score_ceil: float = 1.0


# ── Dimension scorers ──────────────────────────────────────────────────────────

# Each dimension scorer takes (EdgeResult, ConfidenceConfig, context) → float in [0, 1]
_DimScorer = Callable[["EdgeResult", ConfidenceConfig, dict], float]

_NEUTRAL = 0.5   # returned when a dimension cannot be computed


def _score_options_spread(e: EdgeResult, cfg: ConfidenceConfig, _: dict) -> float:
    """Higher options bid-ask spread → less confident in the implied probability."""
    spread = e.match.options_entry.ask_prob - e.match.options_entry.bid_prob
    if cfg.options_spread_max <= 0:
        return _NEUTRAL
    return max(0.0, 1.0 - spread / cfg.options_spread_max)


def _score_pm_depth(e: EdgeResult, cfg: ConfidenceConfig, _: dict) -> float:
    """Tighter PM bid-ask spread → better liquidity → higher confidence."""
    pm_spread = e.match.pm_quote.ask_prob - e.match.pm_quote.bid_prob
    if cfg.pm_spread_max <= 0:
        return _NEUTRAL
    return max(0.0, 1.0 - pm_spread / cfg.pm_spread_max)


def _score_vol_fit(e: EdgeResult, cfg: ConfidenceConfig, ctx: dict) -> float:
    """Lower SVI RMSE → more reliable IV surface → higher confidence."""
    surface: VolSurface | None = ctx.get("surface")
    if surface is None:
        return _NEUTRAL
    smile = surface.get_smile(e.match.matched_expiry)
    if smile is None:
        return _NEUTRAL
    if cfg.vol_rmse_max <= 0:
        return _NEUTRAL
    return max(0.0, 1.0 - smile.fit_rmse / cfg.vol_rmse_max)


def _score_match_quality(e: EdgeResult, cfg: ConfidenceConfig, _: dict) -> float:
    """Direct pass-through of ContractMatcher's quality score (already [0, 1])."""
    return max(0.0, min(1.0, e.match.match_quality))


def _score_edge_persistence(e: EdgeResult, cfg: ConfidenceConfig, _: dict) -> float:
    """Edge that has held over many observations is more likely to be real."""
    if cfg.persistence_max <= 0:
        return _NEUTRAL
    return max(0.0, min(1.0, e.edge_persistence / cfg.persistence_max))


# Ordered list of (scorer_fn, weight_attribute_name) pairs
_DIMENSIONS: list[tuple[_DimScorer, str]] = [
    (_score_options_spread, "w_options_spread"),
    (_score_pm_depth,       "w_pm_depth"),
    (_score_vol_fit,        "w_vol_fit"),
    (_score_match_quality,  "w_match_quality"),
    (_score_edge_persistence, "w_edge_persistence"),
]


# ── Scorer class ───────────────────────────────────────────────────────────────

class ConfidenceScorer:
    """Compute a weighted-average confidence score for an EdgeResult.

    Usage::

        scorer = ConfidenceScorer()
        signal = scorer.score(edge_result, surface=vol_surface)
        # signal.confidence is now in [0, 1]

    The scorer also supports batch operation::

        signals = scorer.score_all(edge_results, surface=vol_surface)
    """

    def __init__(self, config: ConfidenceConfig | None = None) -> None:
        self.config = config or ConfidenceConfig()

    def score(
        self,
        edge: EdgeResult,
        surface: VolSurface | None = None,
    ) -> float:
        """Return confidence score in [cfg.score_floor, cfg.score_ceil].

        Args:
            edge:    EdgeResult from EdgeCalculator.compute().
            surface: VolSurface used for vol-fit quality dimension.

        Returns:
            Confidence score in [0, 1].
        """
        ctx: dict = {"surface": surface}
        cfg = self.config

        weighted_sum = 0.0
        total_weight = 0.0

        breakdown: dict[str, float] = {}

        for scorer_fn, weight_attr in _DIMENSIONS:
            weight = getattr(cfg, weight_attr)
            dim_score = scorer_fn(edge, cfg, ctx)
            weighted_sum += weight * dim_score
            total_weight += weight
            breakdown[scorer_fn.__name__] = round(dim_score, 4)

        raw = weighted_sum / total_weight if total_weight > 0 else _NEUTRAL
        final = max(cfg.score_floor, min(cfg.score_ceil, raw))

        logger.debug(
            "confidence.scored",
            contract=edge.match.pm_tick.contract_id,
            confidence=round(final, 4),
            **breakdown,
        )

        return final

    def score_all(
        self,
        edges: list[EdgeResult],
        surface: VolSurface | None = None,
    ) -> list[float]:
        """Score a batch of EdgeResults."""
        return [self.score(e, surface=surface) for e in edges]

    def dimension_breakdown(
        self,
        edge: EdgeResult,
        surface: VolSurface | None = None,
    ) -> dict[str, float]:
        """Return per-dimension scores for inspection / debugging.

        Returns:
            Dict mapping dimension name to its [0, 1] score.
        """
        ctx: dict = {"surface": surface}
        cfg = self.config
        return {
            scorer_fn.__name__.removeprefix("_score_"): scorer_fn(edge, cfg, ctx)
            for scorer_fn, _ in _DIMENSIONS
        }
