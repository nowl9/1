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
    once per agent; no per-call configuration this round.
    """

    def evaluate(
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
