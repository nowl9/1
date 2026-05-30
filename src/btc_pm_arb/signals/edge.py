"""Edge calculator — computes bid/ask-implied probability edges.

For each matched (options, PM) pair the calculator produces two independent
edges, one for each side of the PM market:

    edge_yes_conservative = options_bid_prob − pm_yes_ask
        ↑ positive → buy YES on PM (PM underprices the event)

    edge_no_conservative  = (1 − options_ask_prob) − pm_no_ask
        ↑ positive → buy NO on PM (PM overprices the event)

Using the *bid-implied* options probability for the YES edge and the
*ask-implied* probability for the NO edge ensures we only signal when the
arbitrage holds against the worst-case options spread — i.e., we are
conservative about what the options market is telling us.

Settlement basis adjustment (optional):
    When a VolSurface is provided the calculator re-derives both edge values
    after adjusting the options probability to the PM contract's settlement
    basis (Deribit 30-min TWAP → Kalshi 60-s RTI or Polymarket spot) using
    the Lévy approximation from basis_adjuster.py.

Edge history:
    A per-contract deque of (timestamp, conservative_edge) observations is
    maintained so the confidence scorer can measure edge persistence.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import numpy as np
import structlog

from btc_pm_arb.models import DataSource
from btc_pm_arb.pricing.vol_surface import VolSurface
from btc_pm_arb.signals.matcher import MatchResult

logger: structlog.BoundLogger = structlog.get_logger(__name__)

TradeSide = Literal["buy_yes", "buy_no"]


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class EdgeResult:
    """All edge information for one matched (options, PM) pair."""

    match: MatchResult

    # ── mid-to-mid edges (no spread consideration) ────────────────────────
    edge_yes_mid: float     # options_mid − pm_yes_mid
    edge_no_mid: float      # (1 − options_mid) − pm_no_mid

    # ── conservative edges (worst-case options bounds vs PM ask prices) ───
    edge_yes_conservative: float   # options_bid_prob − pm_yes_ask
    edge_no_conservative: float    # (1 − options_ask_prob) − pm_no_ask

    # ── settlement-adjusted conservative edges ────────────────────────────
    adjusted_edge_yes: float       # after basis correction
    adjusted_edge_no: float        # after basis correction

    # ── best actionable signal ────────────────────────────────────────────
    best_side: TradeSide | None    # which side has the larger positive edge
    best_conservative_edge: float  # max(adj_yes, adj_no) if > 0 else 0

    # ── fill-adjusted edge (accounts for order book depth) ────────────────
    # None when no order book data available — falls back to conservative edge
    fill_adjusted_edge: float | None = None

    # ── history statistics (set by EdgeCalculator from its deque) ─────────
    edge_history_mean: float = 0.0   # mean conservative edge over recent history
    edge_history_std: float = 0.0    # std dev of recent edge
    edge_persistence: float = 0.0    # mean/std (Sharpe-like ratio, capped at 1)

    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Calculator ────────────────────────────────────────────────────────────────

class EdgeCalculator:
    """Compute and track bid/ask edges for matched prediction-market contracts.

    Usage::

        calc = EdgeCalculator()
        edge = calc.compute(match_result, surface=vol_surface)
        history = calc.get_history("contract-id")
    """

    def __init__(self, history_size: int = 200) -> None:
        self._history_size = history_size
        # contract_id → deque of (timestamp, conservative_edge) tuples
        self._history: dict[str, deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )

    # ── public ───────────────────────────────────────────────────────────

    def compute(
        self,
        match: MatchResult,
        surface: VolSurface | None = None,
    ) -> EdgeResult:
        """Compute edge for a single MatchResult.

        Args:
            match:   Output of ContractMatcher.match().
            surface: VolSurface for settlement basis adjustment.  When None,
                     the adjusted edges equal the conservative edges.
        """
        entry = match.options_entry
        pm = match.pm_quote

        # The pricer always emits P(S_T > K) (probability ABOVE the strike).
        # The PM YES leg, however, can pay on "above" or "below" the strike;
        # ``pm.direction`` carries the parsed polarity.  ``_model_yes_prob``
        # maps the options P(above) bid/mid/ask onto the probability of the
        # PM's YES event (complementing for "below"), so a below-contract is
        # compared against (1 - P(above)) instead of P(above).
        ops_mid = entry.mid_prob
        ops_bid = entry.bid_prob
        ops_ask = entry.ask_prob
        my_bid, my_mid, my_ask = _model_yes_prob(pm.direction, ops_bid, ops_mid, ops_ask)

        # PM prices — YES side
        pm_yes_ask = pm.ask_prob                             # cost to buy YES
        pm_yes_bid = pm.bid_prob                             # receive when selling YES
        pm_yes_mid = (pm_yes_bid + pm_yes_ask) / 2.0

        # PM prices — NO side
        # Prefer explicit no_ask if available; fallback to complement of yes_bid
        pm_no_ask_raw = match.pm_tick.no_ask
        pm_no_bid_raw = match.pm_tick.no_bid
        pm_no_ask = pm_no_ask_raw if pm_no_ask_raw is not None else (1.0 - pm_yes_bid)
        pm_no_bid = pm_no_bid_raw if pm_no_bid_raw is not None else (1.0 - pm_yes_ask)
        pm_no_mid = (pm_no_bid + pm_no_ask) / 2.0

        # ── mid-to-mid edges ──────────────────────────────────────────────
        edge_yes_mid = my_mid - pm_yes_mid
        edge_no_mid = (1.0 - my_mid) - pm_no_mid

        # ── conservative edges ────────────────────────────────────────────
        # YES: use lower model-yes bound (bad for us) vs higher PM ask (bad for us)
        edge_yes_cons = my_bid - pm_yes_ask
        # NO: use upper model-yes bound (bad for us) vs higher PM no-ask (bad for us)
        edge_no_cons = (1.0 - my_ask) - pm_no_ask

        # ── settlement basis adjustment ───────────────────────────────────
        adj_yes, adj_no = self._apply_basis(
            match, edge_yes_cons, edge_no_cons, ops_bid, ops_ask, pm_yes_ask, pm_no_ask, surface
        )

        # ── best side ─────────────────────────────────────────────────────
        best_side, best_edge = _pick_best_side(adj_yes, adj_no)

        # ── fill-adjusted edge ────────────────────────────────────────────
        # Pass the direction-mapped model-yes bounds so the fill edge is
        # polarised the same way as the conservative edge.
        fill_adj = _compute_fill_adjusted_edge(match, best_side, my_bid, my_ask)

        # ── update history ─────────────────────────────────────────────────
        contract_id = match.pm_tick.contract_id
        self._history[contract_id].append((datetime.now(timezone.utc), best_edge))
        h_mean, h_std, h_persistence = self._history_stats(contract_id)

        logger.debug(
            "edge.computed",
            contract=contract_id,
            best_side=best_side,
            conservative_edge=round(best_edge, 4),
            adj_yes=round(adj_yes, 4),
            adj_no=round(adj_no, 4),
            fill_adjusted=round(fill_adj, 4) if fill_adj is not None else None,
        )

        return EdgeResult(
            match=match,
            edge_yes_mid=edge_yes_mid,
            edge_no_mid=edge_no_mid,
            edge_yes_conservative=edge_yes_cons,
            edge_no_conservative=edge_no_cons,
            adjusted_edge_yes=adj_yes,
            adjusted_edge_no=adj_no,
            best_side=best_side,
            best_conservative_edge=best_edge,
            fill_adjusted_edge=fill_adj,
            edge_history_mean=h_mean,
            edge_history_std=h_std,
            edge_persistence=h_persistence,
        )

    def get_history(
        self, contract_id: str
    ) -> list[tuple[datetime, float]]:
        """Return recent (timestamp, edge) observations for a contract."""
        return list(self._history.get(contract_id, []))

    def clear_history(self, contract_id: str | None = None) -> None:
        if contract_id is None:
            self._history.clear()
        else:
            self._history.pop(contract_id, None)

    # ── private ───────────────────────────────────────────────────────────

    def _apply_basis(
        self,
        match: MatchResult,
        edge_yes_cons: float,
        edge_no_cons: float,
        ops_bid: float,
        ops_ask: float,
        pm_yes_ask: float,
        pm_no_ask: float,
        surface: VolSurface | None,
    ) -> tuple[float, float]:
        """Re-derive conservative edges after settlement basis adjustment.

        The cache stores probabilities derived from BS using Deribit IV (which
        implicitly reflects the Deribit TWAP settlement).  We adjust these to
        the PM contract's settlement basis (Polymarket spot or Kalshi RTI).

        When surface is None or adjustment fails, returns the raw conservative
        edges unchanged.
        """
        if surface is None:
            return edge_yes_cons, edge_no_cons

        smile = surface.get_smile(match.matched_expiry)
        if smile is None or smile.params is None:
            return edge_yes_cons, edge_no_cons

        sigma = smile.iv(match.matched_strike)
        if sigma is None or sigma <= 0:
            return edge_yes_cons, edge_no_cons

        forward = smile.forward
        K = match.matched_strike
        T = smile.time_to_expiry

        if forward <= 0 or T <= 0:
            return edge_yes_cons, edge_no_cons

        source = match.pm_tick.source
        if source == DataSource.KALSHI:
            settlement_type = "kalshi_rti"
        elif source == DataSource.POLYMARKET:
            settlement_type = "polymarket_spot"
        else:
            return edge_yes_cons, edge_no_cons

        from btc_pm_arb.pricing.basis_adjuster import BasisAdjuster
        adj = BasisAdjuster()

        # Adjust options probabilities to PM settlement basis
        ops_bid_adj = adj.adjust(ops_bid, forward, K, sigma, T, settlement_type)
        ops_ask_adj = adj.adjust(ops_ask, forward, K, sigma, T, settlement_type)

        # Map the basis-adjusted P(above) bounds onto the PM YES event's
        # probability for this contract's polarity (complement for "below").
        my_bid_adj, _, my_ask_adj = _model_yes_prob(
            match.pm_quote.direction, ops_bid_adj, ops_bid_adj, ops_ask_adj
        )
        adj_edge_yes = my_bid_adj - pm_yes_ask
        adj_edge_no = (1.0 - my_ask_adj) - pm_no_ask

        return adj_edge_yes, adj_edge_no

    def _history_stats(
        self, contract_id: str
    ) -> tuple[float, float, float]:
        """Return (mean, std, persistence) for a contract's edge history."""
        buf = self._history.get(contract_id)
        if not buf or len(buf) < 2:
            return 0.0, 0.0, 0.0

        values = np.array([e for _, e in buf], dtype=float)
        mean = float(np.mean(values))
        std = float(np.std(values))
        persistence = float(np.clip(mean / max(std, 1e-4), 0.0, 10.0) / 10.0)
        return mean, std, persistence


# ── Helpers ───────────────────────────────────────────────────────────────────

def _model_yes_prob(
    direction: str, p_bid: float, p_mid: float, p_ask: float,
) -> tuple[float, float, float]:
    """Map the options P(above) (bid, mid, ask) onto the PM YES event prob.

    The pricer emits P(S_T > K) = P(above).  For an "above" contract the PM
    YES event IS that probability.  For a "below" contract the PM YES event
    is the complement P(S_T <= K) = 1 - P(above); complementing flips the
    bid/ask bounds (the lower bound on P(below) is 1 - upper bound on
    P(above)) so the conservative ordering bid <= ask is preserved.
    """
    if direction == "below":
        return 1.0 - p_ask, 1.0 - p_mid, 1.0 - p_bid
    return p_bid, p_mid, p_ask


def _pick_best_side(
    adj_yes: float, adj_no: float
) -> tuple[TradeSide | None, float]:
    """Select the trade side with the larger positive adjusted edge."""
    if adj_yes <= 0 and adj_no <= 0:
        return None, 0.0
    if adj_yes >= adj_no:
        return "buy_yes", max(adj_yes, 0.0)
    return "buy_no", max(adj_no, 0.0)


def fill_adjusted_price(
    order_book: list[tuple[float, float]],
    size_usd: float,
) -> float | None:
    """Walk an order book to compute the volume-weighted average fill price.

    Args:
        order_book: List of (price, size_usd) levels sorted by price ascending
                    (for YES buys: cheapest ask first).
        size_usd:   Total notional to fill in USD.

    Returns:
        VWAP fill price in [0, 1], or None if the book cannot fill ``size_usd``.
    """
    if not order_book or size_usd <= 0:
        return None
    remaining = size_usd
    cost = 0.0
    for price, level_size in order_book:
        if remaining <= 0:
            break
        take = min(remaining, level_size)
        cost += price * take
        remaining -= take
    if remaining > 1e-9:
        return None   # book too thin to fill the full size
    return cost / size_usd


def _compute_fill_adjusted_edge(
    match: MatchResult,
    best_side: TradeSide | None,
    ops_bid: float,
    ops_ask: float,
) -> float | None:
    """Compute fill-adjusted edge using the PM order book if available.

    Returns None when no order book data is present (caller falls back to
    the conservative edge).
    """
    if best_side is None:
        return None

    if best_side == "buy_yes":
        book = match.pm_tick.order_book_yes
        if not book:
            return None
        # Use the same size heuristic as risk.size_for_signal's default
        fill_price = fill_adjusted_price(book, size_usd=200.0)
        if fill_price is None:
            return None
        return ops_bid - fill_price
    else:
        book = match.pm_tick.order_book_no
        if not book:
            return None
        fill_price = fill_adjusted_price(book, size_usd=200.0)
        if fill_price is None:
            return None
        return (1.0 - ops_ask) - fill_price
