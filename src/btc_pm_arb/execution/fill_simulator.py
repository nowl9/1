"""Fill simulator — evaluates whether a paper order would have filled.

Round 8 implements the simplest viable model: at-or-better-than-best limit
against a captured order-book snapshot.  One-shot, full-or-nothing.

Honest accounting of the production bias
----------------------------------------
``OrderManager.place()`` (orders.py:351–354) sets, for a buy_yes signal,
``limit_price = signal.pm_quote.ask_prob``.  ``ask_prob`` is built in
``_pm_tick_to_quote`` (matcher.py:236) directly from ``tick.yes_ask`` —
the same tick this simulator captures as its order-book snapshot.

**Within a single scan tick, ``limit_price == best_yes_ask`` by
construction.**  Every signal that survives filters and reaches the
simulator therefore lands in the marketable branch and gets filled at
ask.  In production:

* The :data:`non_marketable_dropped` and :data:`below_book` reasons are
  unreachable — there is no slippage haircut and no signal-to-fill
  latency in Round 8.
* Every passing signal becomes a recorded fill at the ask price observed
  in the same scan tick.  This is the dataset shape Round 9 calibration
  will see.
* Round 9 calibration must therefore introduce a slippage / queue-position
  / latency model before the no-fill branches mean anything.

The four no-fill branches (:data:`non_marketable_dropped`,
:data:`below_book`, :data:`no_opposite_quote`, :data:`empty_book`) exist
as defensive code paths against (a) a future round that haircuts
``limit_price`` or introduces latency, (b) malformed/stale ticks that
slip through, and (c) the test scenarios in
``tests/test_execution/test_fill_simulator.py`` that drive each branch
deliberately.  Without those tests, a future round that introduced a
haircut and shipped a simulator only ever exercising "fill at ask" would
go unnoticed.

Documented simplifications (Round 9 punch list)
-----------------------------------------------
* **Top-of-book only — depth fields are captured but not consumed.**
  The simulator evaluates against ``snapshot.yes_ask`` / ``snapshot.no_ask``
  directly.  ``snapshot.order_book_yes`` / ``order_book_no`` are persisted
  on the :class:`PaperOrderRecord` so Round 9 calibration has the depth
  data available, but their per-feed interpretation differs (Kalshi:
  bid levels on each side, asks derived from the complementary side;
  Polymarket: separate bid/ask books) and the simulator stays
  dialect-agnostic by ignoring them.  Round 9 extends the simulator
  with feed-specific depth-walking once calibration shows it matters.
* No partial fills — full ``size_usd`` fills or nothing.
* No queue-position simulation — orders at-or-better-than-bid never fill.
* No time-to-fill model — marketable orders fill in the same scan tick.
* No latency model — book observed at signal time IS the book the order
  trades against.
* Polymarket signals are still short-circuited at
  ``OrderManager.place`` (orders.py:339–349); the simulator never sees
  them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from btc_pm_arb.execution.paper_ledger import BookLevel, PaperFillRecord


# ── Snapshot ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BookSnapshot:
    """Order-book observation captured at signal-generation time.

    Persisted on the :class:`PaperOrderRecord` and re-hydrated for the
    simulator at fill-evaluation time.  Round 8 simulator uses the
    top-of-book ``yes_ask`` / ``no_ask`` fields only.  ``order_book_yes`` /
    ``order_book_no`` are captured here for Round 9 calibration but not
    consumed at fill-evaluation time — see the simulator's module docstring
    for the dialect-agnosticism rationale.
    """

    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    order_book_yes: tuple[BookLevel, ...] = field(default_factory=tuple)
    order_book_no: tuple[BookLevel, ...] = field(default_factory=tuple)

    @classmethod
    def from_order_record(cls, record: "PaperOrderRecordLike") -> "BookSnapshot":
        """Build a snapshot from a :class:`PaperOrderRecord` instance.

        Uses a structural type so tests can pass simple objects without
        constructing a full ``PaperOrderRecord``.
        """
        return cls(
            yes_bid=record.pm_yes_bid,
            yes_ask=record.pm_yes_ask,
            no_bid=record.pm_no_bid,
            no_ask=record.pm_no_ask,
            order_book_yes=tuple(record.order_book_yes),
            order_book_no=tuple(record.order_book_no),
        )

    @classmethod
    def from_tick(cls, tick: "PredictionMarketTickLike") -> "BookSnapshot":
        """Build a snapshot directly from a :class:`models.PredictionMarketTick`.

        The rejection-path shadow fill (main.py) reuses the SAME book-walk as
        placed orders, but a rejected contract never becomes a
        :class:`PaperOrderRecord`.  This adapter converts the originating tick
        using the IDENTICAL ``(price, size) -> BookLevel`` mapping
        ``_build_paper_order_record`` applies for placed orders, so the
        snapshot fed to :meth:`evaluate` is constructed the same way on both
        the passing-order and rejection paths.  Structural type so callers and
        tests can pass any object with the tick's field shape.
        """
        return cls(
            yes_bid=tick.yes_bid,
            yes_ask=tick.yes_ask,
            no_bid=tick.no_bid,
            no_ask=tick.no_ask,
            order_book_yes=tuple(
                BookLevel(price=p, size_usd=s) for p, s in tick.order_book_yes
            ),
            order_book_no=tuple(
                BookLevel(price=p, size_usd=s) for p, s in tick.order_book_no
            ),
        )


# Structural protocol for ``BookSnapshot.from_order_record`` — declared as
# a plain class for typing simplicity (Protocol would require importing
# typing.Protocol; a duck-typed annotation is fine here).
class PaperOrderRecordLike:
    pm_yes_bid: float | None
    pm_yes_ask: float | None
    pm_no_bid: float | None
    pm_no_ask: float | None
    order_book_yes: list[BookLevel]
    order_book_no: list[BookLevel]


# Structural protocol for ``BookSnapshot.from_tick`` — the tick's order-book
# fields are raw ``(price, size_usd)`` tuples (converted to ``BookLevel`` in
# the classmethod), unlike ``PaperOrderRecordLike`` whose levels are already
# ``BookLevel`` instances.
class PredictionMarketTickLike:
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    order_book_yes: list[tuple[float, float]]
    order_book_no: list[tuple[float, float]]


# ── Evaluation result ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FillEvaluation:
    """Result of running the simulator against one (order, snapshot) pair.

    ``fill_price`` is None when ``outcome == "no_fill"``.  ``reason`` is
    one of the documented simulator reasons (see module docstring).
    """

    outcome: Literal["full", "partial", "no_fill"]
    fill_price: float | None
    fill_size_usd: float
    reason: str


# ── Simulator ─────────────────────────────────────────────────────────────────


class FillSimulator:
    """Evaluate paper orders against captured order-book snapshots.

    Stateless — every call to :meth:`evaluate` is independent.  Construct
    once per agent.

    Two fill models (Fork 1, plan section 4.1)
    ------------------------------------------
    * ``book_walk=False`` (default): legacy top-of-book, full-or-nothing.
      Evaluates ``limit_price`` against ``snapshot.yes_ask`` / ``no_ask``
      only; depth is ignored.  Kept as the default so existing callers and
      tests see unchanged behaviour.
    * ``book_walk=True``: walks the captured ``order_book_yes`` /
      ``order_book_no`` depth (the same ask-side, cheapest-first levels the
      fill-adjusted-edge calculator consumes), accumulating fills at or
      below ``limit_price`` up to ``size_usd``.  Yields PARTIAL fills when
      depth is thin and an explicit, diagnosable no_fill (with a reason) for
      an empty book or a limit below the whole book -- never a silent
      optimistic full fill.  This is the paper-mode model: fill realism IS
      the scalping-strategy validation, not a later refinement.
    """

    def __init__(self, book_walk: bool = False) -> None:
        self._book_walk = book_walk

    def evaluate(
        self,
        *,
        side: Literal["yes", "no"],
        limit_price: float,
        size_usd: float,
        snapshot: BookSnapshot,
    ) -> FillEvaluation:
        """Return whether the paper order fills, and at what price.

        Dispatches to the book-walking model when constructed with
        ``book_walk=True``; otherwise the legacy top-of-book model.
        """
        if self._book_walk:
            return self._evaluate_book_walk(
                side=side, limit_price=limit_price, size_usd=size_usd,
                snapshot=snapshot,
            )
        return self._evaluate_top_of_book(
            side=side, limit_price=limit_price, size_usd=size_usd,
            snapshot=snapshot,
        )

    # -- Book-walking model (Fork 1) --------------------------------------------

    def _evaluate_book_walk(
        self,
        *,
        side: Literal["yes", "no"],
        limit_price: float,
        size_usd: float,
        snapshot: BookSnapshot,
    ) -> FillEvaluation:
        """Walk captured depth, accumulating fills at-or-below ``limit_price``.

        Levels are the ask-side book for the order's side
        (``order_book_yes`` for a YES buy, ``order_book_no`` for a NO buy)
        -- the same shape and cheapest-first convention
        ``edge.fill_adjusted_price`` uses.  Sorted ascending defensively so
        the walk consumes the best price first regardless of capture order.

        Outcomes (never a silent full fill):
          * ``empty_book``        -- no captured depth on this side.
          * ``limit_below_book``  -- depth exists but every level is priced
                                     above the limit (nothing marketable).
          * ``book_walk_partial`` -- only part of ``size_usd`` is available
                                     at-or-below the limit.
          * ``book_walk_full``    -- the full size fills at the depth VWAP.
        """
        levels = (
            snapshot.order_book_yes if side == "yes" else snapshot.order_book_no
        )
        if not levels:
            return FillEvaluation(
                outcome="no_fill",
                fill_price=None,
                fill_size_usd=0.0,
                reason="empty_book",
            )

        # Cheapest ask first.  Defensive sort -- captured order is documented
        # ascending but we do not rely on it.
        ordered = sorted(levels, key=lambda lvl: lvl.price)
        filled = 0.0
        cost = 0.0
        for lvl in ordered:
            if filled + 1e-9 >= size_usd:
                break
            if lvl.price > limit_price:
                # Ascending price -- no cheaper level remains to lift.
                break
            take = min(size_usd - filled, lvl.size_usd)
            if take <= 0.0:
                continue
            cost += lvl.price * take
            filled += take

        if filled <= 0.0:
            # Depth present but the whole book sits above the limit.
            return FillEvaluation(
                outcome="no_fill",
                fill_price=None,
                fill_size_usd=0.0,
                reason="limit_below_book",
            )

        vwap = cost / filled
        if filled + 1e-9 >= size_usd:
            return FillEvaluation(
                outcome="full",
                fill_price=vwap,
                fill_size_usd=size_usd,
                reason="book_walk_full",
            )
        return FillEvaluation(
            outcome="partial",
            fill_price=vwap,
            fill_size_usd=filled,
            reason="book_walk_partial",
        )

    # -- Legacy top-of-book model (full-or-nothing) -----------------------------

    def _evaluate_top_of_book(
        self,
        *,
        side: Literal["yes", "no"],
        limit_price: float,
        size_usd: float,
        snapshot: BookSnapshot,
    ) -> FillEvaluation:
        """Return whether the paper order fills, and at what price.

        See module docstring for the production-bias caveat: in practice
        every signal lands in the marketable branch, because
        ``limit_price`` and the snapshot come from the same PM tick.  The
        no-fill branches are defensive and reachable only via stale or
        haircut-modified inputs.
        """
        # Top-of-book only this round; depth fields on the snapshot are
        # captured-but-not-consumed (see module docstring).
        if side == "yes":
            best_ask = snapshot.yes_ask
            best_bid = snapshot.yes_bid
        else:
            best_ask = snapshot.no_ask
            best_bid = snapshot.no_bid

        # Empty book on both sides — nothing to evaluate against
        if best_ask is None and best_bid is None:
            return FillEvaluation(
                outcome="no_fill",
                fill_price=None,
                fill_size_usd=0.0,
                reason="empty_book",
            )

        # One-sided book — we have a bid but no ask to lift
        if best_ask is None:
            return FillEvaluation(
                outcome="no_fill",
                fill_price=None,
                fill_size_usd=0.0,
                reason="no_opposite_quote",
            )

        # Marketable — limit at-or-above the best ask we see; fill at ask
        # (price improvement on the taker, honest because the resting
        # opposite is what Kalshi would match against).
        if limit_price >= best_ask:
            return FillEvaluation(
                outcome="full",
                fill_price=best_ask,
                fill_size_usd=size_usd,
                reason="marketable_against_book",
            )

        # Non-marketable but still inside the spread — defensive branch,
        # unreachable in production with a same-tick snapshot.
        if best_bid is not None and limit_price >= best_bid:
            return FillEvaluation(
                outcome="no_fill",
                fill_price=None,
                fill_size_usd=0.0,
                reason="non_marketable_dropped",
            )

        # Below the bid — defensive branch, unreachable in production
        # with a same-tick snapshot.
        return FillEvaluation(
            outcome="no_fill",
            fill_price=None,
            fill_size_usd=0.0,
            reason="below_book",
        )

    def build_fill_record(
        self,
        *,
        client_order_id: str,
        evaluation: FillEvaluation,
        filled_at: datetime,
        fees_usd: float = 0.0,
    ) -> PaperFillRecord:
        """Materialise an evaluation into a :class:`PaperFillRecord`."""
        return PaperFillRecord(
            client_order_id=client_order_id,
            filled_at=filled_at,
            fill_price=evaluation.fill_price,
            fill_size_usd=evaluation.fill_size_usd,
            fill_outcome=evaluation.outcome,
            simulator_reason=evaluation.reason,
            fees_usd=fees_usd,
        )
