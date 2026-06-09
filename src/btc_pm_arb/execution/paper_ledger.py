"""Paper-trading ledger — append-only JSONL storage for would-be-trade records.

Round 8 introduces this module as the project's first persistence layer.  It
records the dry-run order intents, fill simulations, and Kalshi settlements
that the agent would have executed in live mode, so Round 9 can calibrate
strategy parameters against weeks-to-months of accumulated outcomes.

Storage choice — append-only JSONL, one file per record kind
------------------------------------------------------------
Four files under ``settings.paper_ledger_dir`` (default ``./paper_ledger/``):

  - ``orders.jsonl``       — :class:`PaperOrderRecord`
  - ``fills.jsonl``        — :class:`PaperFillRecord`
  - ``settlements.jsonl``  — :class:`PaperSettlementRecord`
  - ``rejections.jsonl``   — :class:`PaperRejectionRecord` (per-event
    filter-rejection log; Round 9c addition feeding tail_funnel and
    Round 9d2 univariate-cuts analysis).

Each line is one ``model_dump_json()``-encoded record.  The choice of JSONL
over SQLite or SQLAlchemy was deliberate (Round 8 plan §a):

* Matches the project's existing patterns — every other stateful component
  (``PositionTracker``, ``SettlementMonitor``, ``OrderManager``) is in-memory.
  Introducing a DB would carry schema-migration weight that does not exist
  today.  Round 9 calibration can ``json.loads`` the JSONL into pandas with
  zero ceremony, or migrate to SQLite then if needed (the
  ``schema_version`` field on every record is the discriminator a future
  migration tool will dispatch on).
* Stdlib only — no new external dependencies.
* Crash-safe — every append calls ``os.fsync`` after the write/flush; the
  reader's "skip-and-warn-with-counter" policy (see below) handles the worst
  realistic corruption (truncated trailing line on process kill).

Reader policy on parse errors — skip-and-warn-with-counter
----------------------------------------------------------
A malformed line in the middle of a file is logged at WARNING under
``paper_ledger.parse_error`` with the line number, file path, and the
first 200 chars of the payload, and the read continues.  Two counters
are maintained on the :class:`PaperLedger` instance and surfaced through
:meth:`PaperLedger.health`:

  - ``n_records_loaded`` — successful reads since instantiation
  - ``n_parse_errors``   — malformed lines skipped since instantiation

The reader does NOT abort on a single bad line (would block agent startup
on transient corruption — bad tradeoff for research data) and does NOT
move the bad line to a quarantine file (overhead not justified at our
volume; the WARNING log carries the payload preview if forensics are
needed).  Operators sanity-check the load by comparing the two counters
against the raw line count.

fsync on the event loop — accepted, inline
------------------------------------------
``os.fsync`` is a blocking syscall that runs synchronously on the
asyncio scan task.  Typical SSD ``fsync`` latency is 1–5 ms; paper-ledger
appends happen at most once per passing signal per 5-second scan tick
(typical: 0–3 per tick).  The 500 ms arbitrage latency budget is for the
live-execution path (Round 9+), not the dry-run paper-ledger path; the
scan task already does work in the tens of milliseconds.

We accept the inline blocking rather than offloading to
``loop.run_in_executor``.  Rationale: avoids a thread-pool dependency and
the per-append context-switch cost, in exchange for a few-millisecond
blocking window on a code path that is not latency-critical.  If a future
round introduces a fast-path that needs sub-100ms cycles for paper-ledger
appends, this is the trivial swap point.

Order-book depth fields — persisted but not consumed in Round 8
---------------------------------------------------------------
:class:`PaperOrderRecord` carries ``order_book_yes`` / ``order_book_no``
(as ``list[BookLevel]``) plus the four top-of-book scalars
(``pm_yes_bid``, ``pm_yes_ask``, ``pm_no_bid``, ``pm_no_ask``).  The
Round 8 :class:`fill_simulator.FillSimulator` evaluates against the
top-of-book scalars only.  The depth lists are persisted here so Round 9
calibration has them available without needing to reconstruct from logs.

Why the simulator does not consume depth this round: the per-feed
interpretation of ``order_book_*`` differs (Kalshi: bid levels on each
side, with asks derived from the complementary side via
``yes_ask = 1 - max(no_bid)``; Polymarket: separate bid/ask books).  A
dialect-agnostic depth-walk is wrong for both; per-feed walks are
deferred to Round 9 once calibration shows they matter.  Storing the raw
levels now means that decision can be made against real recorded data
rather than estimated from after-the-fact reconstruction.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal, TypeVar

import structlog
from pydantic import BaseModel, ConfigDict, Field

from btc_pm_arb.models import DataSource

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# ── Sub-models ────────────────────────────────────────────────────────────────

# Current schema version for all paper-ledger records.  Bumped only when the
# wire format changes incompatibly; future migration tools dispatch on the
# ``schema_version`` field on each record rather than sniffing field
# presence.
_SCHEMA_VERSION: int = 1


class BookLevel(BaseModel):
    """One level of an order book — price and size in USD.

    Used in :class:`PaperOrderRecord` to capture the order-book snapshot at
    signal-generation time.  Defined as a typed sub-model (not a tuple) so
    Round 9 readers using raw ``json.loads`` see named fields
    (``level["price"]``, ``level["size_usd"]``) rather than positional
    arrays — protects against a future field reordering breaking pandas
    pipelines silently.
    """

    model_config = ConfigDict(frozen=True)

    price: float = Field(ge=0.0, le=1.0)
    size_usd: float = Field(ge=0.0)


# ── Record models ─────────────────────────────────────────────────────────────


class PaperOrderRecord(BaseModel):
    """Order-intent record — written exactly once at order-intent time.

    Captures everything Round 9 calibration will need to reconstruct the
    moment of the would-be-trade decision: the signal that triggered it,
    the match-quality context, the top-of-book bid/ask the simulator
    actually evaluated against, and the full order-book depth (persisted
    but not consumed by the Round 8 simulator — see module docstring's
    "Order-book depth fields" section for the dialect-agnosticism
    rationale).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["order"] = "order"
    schema_version: int = _SCHEMA_VERSION

    # Run identity (build step 4): every ledger append is stamped with the
    # run's id + mode (live|replay) so runs are separable and joinable.
    # Optional/defaulted so pre-step-4 records round-trip at schema_version 1
    # and direct-construction (tests) needn't supply them.
    run_id: str = ""
    mode: str = "live"

    # ── Identity ──────────────────────────────────────────────────────────────
    client_order_id: str
    signal_fingerprint: str
    created_at: datetime

    # ── Order parameters ──────────────────────────────────────────────────────
    platform: DataSource
    contract_id: str
    side: Literal["yes", "no"]
    size_usd: float
    limit_price: float = Field(ge=0.0, le=1.0)

    # ── From ArbitrageSignal (point-in-time snapshot — signal may mutate) ─────
    raw_edge: float
    adjusted_edge: float
    fill_adjusted_edge: float | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    vol_regime: str
    # Per-feed staleness at order time (ms).  Values are nullable: None
    # means "feed entry present in dict but no staleness measurement
    # available" (e.g. Polymarket queried but no tick observed yet);
    # absence from the dict means "feed wasn't queried at all."  The
    # distinction matters for Round 9 calibration — see Commit-2
    # self-audit note about not prematurely tightening this schema.
    # Without nullable values, a record like {"polymarket": None, ...}
    # writes successfully through pydantic on Python construction but
    # fails to round-trip from JSONL, breaking the load-bearing replay
    # invariant from Commit 1's idempotency test.
    feed_staleness_ms: dict[str, float | None] = Field(default_factory=dict)

    # ── From MatchResult ──────────────────────────────────────────────────────
    strike_gap_pct: float
    expiry_gap_hours: float
    match_quality: float

    # ── Order-book snapshot from originating PredictionMarketTick ─────────────
    pm_yes_bid: float | None = None
    pm_yes_ask: float | None = None
    pm_no_bid: float | None = None
    pm_no_ask: float | None = None
    order_book_yes: list[BookLevel] = Field(default_factory=list)
    order_book_no: list[BookLevel] = Field(default_factory=list)

    # ── Settlement scheduling ─────────────────────────────────────────────────
    expiry: datetime

    # ── Contract threshold + polarity (deterministic benchmark settlement) ────
    # Captured at order time so the benchmark settler (build step 3) can
    # evaluate the terminal-digital predicate (BTC above/below strike at
    # expiry) WITHOUT a live oracle call.  Optional/defaulted so pre-step-3
    # records and tests that omit them round-trip cleanly at schema_version 1.
    strike: float | None = None
    direction: Literal["above", "below"] = "above"

    # ── Mode flag (forward-compat: always True for Round 8) ───────────────────
    dry_run: bool = True


class PaperIntentRecord(BaseModel):
    """Shadow no-op order intent (build step 6; plan 3.6; criterion 2).

    The order the LIVE routing path WOULD have submitted to the venue,
    captured as an audit record BEFORE the :class:`fill_simulator.FillSimulator`
    evaluates the fill -- and WITHOUT submitting anything.  ``submitted`` is
    always ``False`` in paper mode: the live path would submit; we never do.

    Distinct from :class:`PaperOrderRecord` (the paper order the simulator
    then fills): this is the venue-submission payload the live execution path
    produces (venue / side / limit / size + the top-of-book snapshot it was
    formed against), recorded so the ledger carries the exact intent live
    execution would emit, captured without execution.  Shares
    ``client_order_id`` with the order/fill so the three join.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["intent"] = "intent"
    schema_version: int = _SCHEMA_VERSION

    # Run identity (build step 4): stamped onto every append.
    run_id: str = ""
    mode: str = "live"

    client_order_id: str
    created_at: datetime
    platform: DataSource
    contract_id: str
    side: Literal["yes", "no"]
    size_usd: float
    limit_price: float = Field(ge=0.0, le=1.0)

    # Top-of-book the intent was formed against (the snapshot the live path
    # would have priced its submission off).
    pm_yes_bid: float | None = None
    pm_yes_ask: float | None = None
    pm_no_bid: float | None = None
    pm_no_ask: float | None = None

    # Always False in paper mode -- the live path WOULD submit; we record the
    # intent and submit nothing.  Kept as a field (not a constant) so a future
    # live-execution round can flip it without a schema change.
    submitted: bool = False


class PaperFillRecord(BaseModel):
    """Fill-evaluation record — written immediately after the simulator runs.

    One :class:`PaperFillRecord` per :class:`PaperOrderRecord` in Round 8
    (one-shot full-or-no-fill simulator).  Schema supports a future
    multi-fill model via the ``fill_outcome="partial"`` value.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["fill"] = "fill"
    schema_version: int = _SCHEMA_VERSION

    # Run identity (build step 4): every ledger append is stamped with the
    # run's id + mode (live|replay) so runs are separable and joinable.
    # Optional/defaulted so pre-step-4 records round-trip at schema_version 1
    # and direct-construction (tests) needn't supply them.
    run_id: str = ""
    mode: str = "live"

    client_order_id: str
    filled_at: datetime
    fill_price: float | None = Field(default=None, ge=0.0, le=1.0)
    fill_size_usd: float = Field(default=0.0, ge=0.0)
    fill_outcome: Literal["full", "partial", "no_fill"]
    simulator_reason: str
    fees_usd: float = 0.0


class PaperSettlementRecord(BaseModel):
    """Terminal Kalshi-settlement record.

    Written when the paper-settlement poller (Commit 2) detects a contract
    has resolved.  Self-contained: includes the entry price and theoretical
    edge from the originating order so the settlements file is independently
    analysable without joining against orders.jsonl.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["settlement"] = "settlement"
    schema_version: int = _SCHEMA_VERSION

    # Run identity (build step 4): every ledger append is stamped with the
    # run's id + mode (live|replay) so runs are separable and joinable.
    # Optional/defaulted so pre-step-4 records round-trip at schema_version 1
    # and direct-construction (tests) needn't supply them.
    run_id: str = ""
    mode: str = "live"

    client_order_id: str
    contract_id: str
    platform: DataSource
    side: Literal["yes", "no"]
    settled_at: datetime

    # Price at which the contract resolved (1.0 = YES won, 0.0 = NO won)
    settlement_price: float = Field(ge=0.0, le=1.0)
    # Effective payout per unit notional, given the position's side
    payout_price: float = Field(ge=0.0, le=1.0)

    entry_price: float = Field(ge=0.0, le=1.0)
    size_usd: float = Field(ge=0.0)
    realized_pnl: float
    fees_usd: float = 0.0
    outcome: Literal["win", "loss", "push"]
    theoretical_edge: float
    expiry: datetime


class PaperRejectionRecord(BaseModel):
    """Per-event filter-rejection record — written for every rejected edge.

    Round 9c addition.  Persisted to ``rejections.jsonl``.  Captures the
    per-event time series the in-memory ``SignalFilter.rejection_counts``
    (cumulative only) cannot — required for tail_funnel's rolling 1h/6h
    reject-rate surface during 9c, and read directly by 9d2 for
    univariate cuts on ``best_conservative_edge`` vs ``reason_key``.

    The full reason string is preserved alongside the stable bucket key:
    bucket key drives counters and grouping (matches the existing
    dashboard ``reject_<key>`` keys); full reason carries the numeric
    detail (e.g. ``"conservative_edge 0.0050 < min 0.01"``) for forensics.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["rejection"] = "rejection"
    schema_version: int = _SCHEMA_VERSION

    # Run identity (build step 4): every ledger append is stamped with the
    # run's id + mode (live|replay) so runs are separable and joinable.
    # Optional/defaulted so pre-step-4 records round-trip at schema_version 1
    # and direct-construction (tests) needn't supply them.
    run_id: str = ""
    mode: str = "live"

    timestamp: datetime
    contract_id: str
    platform: DataSource
    reason_key: str
    full_reason: str

    # Edge value at rejection time — 9d2 reads this for univariate cuts on
    # adjusted edge vs reject reason.  Source: EdgeResult.best_conservative_edge.
    best_conservative_edge: float

    # Same shape as PaperOrderRecord.vol_regime ("low" / "normal" / "high");
    # source-side is rv_tracker.current_regime().value.
    vol_regime: str

    # ── Shadow fill-adjusted edge (rejection-path measurement infra) ──────────
    # Computed ONLY for near-floor edge-economics rejections (reason_key in the
    # edge/threshold set, a positive best_side, and best_conservative_edge above
    # the noise floor) by invoking the SAME book-walking
    # ``fill_simulator.FillSimulator`` placed orders use — NOT a second, more
    # optimistic fill model.  ``fill_adjusted_edge`` is the model fair value
    # minus the depth-walked fill price, so the near-floor band carries an
    # honest fill estimate (e.g. +3% theoretical collapsing negative against the
    # 0.99 wall) instead of only the 1-2 contracts that clear the floor.
    #
    # None for rejections outside that band (no meaningful fill) AND for
    # no_fill walks (empty / limit-below book) — never a manufactured positive
    # edge.  ``fill_simulator_reason`` / ``fill_outcome`` / ``fill_size_usd``
    # mirror the PaperFillRecord fields so a partial walk is diagnosable.
    #
    # All optional/defaulted -> records round-trip at schema_version 1; this is
    # the same additive precedent as run_id / mode / strike / direction (no
    # wire-format bump, future migration tools dispatch on schema_version).
    fill_adjusted_edge: float | None = None
    fill_simulator_reason: str | None = None
    fill_outcome: Literal["full", "partial", "no_fill"] | None = None
    fill_size_usd: float | None = None

    # ── Chase-adjusted edge (#1: UNCAPPED completion cost) ────────────────────
    # The COMPANION to ``fill_adjusted_edge`` above, NOT a replacement.  Where
    # ``fill_adjusted_edge`` (#2) is the CAPPED passive fill -- the FillSimulator
    # stops at the limit, so it can only fill what is at-or-below the price and
    # is never negative (a thin near-floor book collapses to a partial-fill
    # rate, not a loss) -- ``chase_adjusted_edge`` (#1) is the UNCAPPED
    # completion cost: ``model fair value - edge.fill_adjusted_price(book, size)``
    # walks the WHOLE book to COMPLETE the full size, crossing the 0.99 wall.
    # It is the SAME book-walk (``signals.edge.fill_adjusted_price``) that
    # populates ``orders.jsonl``'s fill_adjusted_edge for placed orders.
    #
    # Because the walk chases through the wall to fill, this value CAN GO
    # NEGATIVE (a +3% theoretical edge collapsing to e.g. -15% once the size is
    # completed against thin near-mid depth).  A NEGATIVE chase_adjusted_edge is
    # a CORRECTLY-REJECTED LOSER: completing that contract would LOSE money, so
    # the floor that rejected it was right.  It is NOT a missed signal and NOT
    # edge left on the table -- do not read it as a trade we could have captured.
    # The value is recorded as-is and never clipped at zero (clipping would
    # manufacture phantom positive edge; the negative IS the measurement).
    #
    # None for rejections outside the near-floor band AND for no_fill walks
    # (whole book too thin to complete the size) -- never an invented edge.
    # Optional/defaulted -> round-trips at schema_version 1, the same additive
    # precedent as the #2 fields above (no wire-format bump).
    chase_adjusted_edge: float | None = None


class PaperRiskBlockRecord(BaseModel):
    """Risk-limit block record -- written when the risk layer vetoes an intent.

    One record per blocked order intent (risk-limit goal, Phase 2).  The
    intent passed every edge/confidence gate but breached a declarative
    cap (``execution/risk_limits.py``), so NO order was placed; this
    record is the only artifact.  Persisted to ``risk_blocks.jsonl``.

    Carries the portfolio numbers the decision was made against
    (run_id-scoped, event-sourced -- see risk_limits module docstring)
    so a block is auditable without re-deriving state.  A persistent
    breach re-blocks on every ~5 s scan while the signal keeps passing
    the edge gates -- one record per blocked intent, by design (the
    dedupe fingerprint is only registered on successful place()).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["risk_block"] = "risk_block"
    schema_version: int = _SCHEMA_VERSION

    # Run identity (build step 4 precedent): stamped onto every append.
    run_id: str = ""
    mode: str = "live"

    timestamp: datetime
    platform: DataSource
    contract_id: str
    side: Literal["yes", "no"]
    size_usd: float

    # The first-breached cap's reason string from check_risk.
    reason: str

    # Portfolio state the caps were evaluated against (run_id-scoped).
    market_position_usd: float
    global_exposure_usd: float
    daily_realized_pnl_usd: float


# ── PaperLedger ───────────────────────────────────────────────────────────────

T = TypeVar("T", bound=BaseModel)


class PaperLedger:
    """Append-only JSONL writer/reader for the three paper-trading streams.

    Usage::

        ledger = PaperLedger("./paper_ledger")
        ledger.append_order(order_record)
        ledger.append_fill(fill_record)
        ledger.append_settlement(settlement_record)

        # On restart:
        for order in ledger.replay_orders():
            ...
        print(ledger.health())   # → {n_records_loaded: ..., n_parse_errors: ...}
    """

    _ORDERS_FILE: str = "orders.jsonl"
    _INTENTS_FILE: str = "intents.jsonl"
    _FILLS_FILE: str = "fills.jsonl"
    _SETTLEMENTS_FILE: str = "settlements.jsonl"
    _REJECTIONS_FILE: str = "rejections.jsonl"
    _RISK_BLOCKS_FILE: str = "risk_blocks.jsonl"

    def __init__(
        self, base_dir: str | Path, *, run_id: str = "", mode: str = "live",
    ) -> None:
        self._base_dir = Path(base_dir)
        # Build step 4: stamped onto every appended record so live vs replay
        # runs are separable + joinable.  Defaults ("", "live") keep
        # round-trip equality for callers that construct a bare
        # PaperLedger(dir) -- the stamp then equals each record's own
        # field defaults.
        self._run_id = run_id
        self._mode = mode
        # Idempotent: mkdir(parents=True, exist_ok=True) is safe on every
        # construction.  Tests use a fresh tmp_path per case.
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._orders_path = self._base_dir / self._ORDERS_FILE
        self._intents_path = self._base_dir / self._INTENTS_FILE
        self._fills_path = self._base_dir / self._FILLS_FILE
        self._settlements_path = self._base_dir / self._SETTLEMENTS_FILE
        self._rejections_path = self._base_dir / self._REJECTIONS_FILE
        self._risk_blocks_path = self._base_dir / self._RISK_BLOCKS_FILE

        # Counters for skip-and-warn-with-counter reader policy.  Surfaced
        # via .health(); operators compare against raw line count to verify
        # load.
        self._n_parse_errors: int = 0
        self._n_records_loaded: int = 0

    # ── Append API ────────────────────────────────────────────────────────────

    def append_order(self, record: PaperOrderRecord) -> None:
        self._append(self._orders_path, record)

    def append_intent(self, record: PaperIntentRecord) -> None:
        """Append a shadow no-op order intent (build step 6).  Submits nothing."""
        self._append(self._intents_path, record)

    def append_fill(self, record: PaperFillRecord) -> None:
        self._append(self._fills_path, record)

    def append_settlement(self, record: PaperSettlementRecord) -> None:
        self._append(self._settlements_path, record)

    def append_rejection(self, record: PaperRejectionRecord) -> None:
        self._append(self._rejections_path, record)

    def append_risk_block(self, record: PaperRiskBlockRecord) -> None:
        """Append a risk-limit block record (risk-limit goal).  No order exists."""
        self._append(self._risk_blocks_path, record)

    def _append(self, path: Path, record: BaseModel) -> None:
        """Append one record as a JSON line, flush, and fsync.

        Stamps run_id + mode (build step 4) onto the record before
        serialising.  model_copy on the frozen record returns a new instance
        with the run fields set; the caller's object is untouched.

        See module docstring for the inline-fsync rationale.
        """
        record = record.model_copy(update={"run_id": self._run_id, "mode": self._mode})
        line = record.model_dump_json()
        # Open/close per append: cleaner than holding file handles open
        # across the agent's lifetime (no leaked-fd risk on crash, no need
        # to coordinate handle lifecycle with shutdown), and the open()
        # call is negligible compared to the fsync that follows.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

    # ── Replay API ────────────────────────────────────────────────────────────

    def replay_orders(self) -> Iterator[PaperOrderRecord]:
        yield from self._replay(self._orders_path, PaperOrderRecord)

    def replay_intents(self) -> Iterator[PaperIntentRecord]:
        yield from self._replay(self._intents_path, PaperIntentRecord)

    def replay_fills(self) -> Iterator[PaperFillRecord]:
        yield from self._replay(self._fills_path, PaperFillRecord)

    def replay_settlements(self) -> Iterator[PaperSettlementRecord]:
        yield from self._replay(self._settlements_path, PaperSettlementRecord)

    def replay_rejections(self) -> Iterator[PaperRejectionRecord]:
        yield from self._replay(self._rejections_path, PaperRejectionRecord)

    def replay_risk_blocks(self) -> Iterator[PaperRiskBlockRecord]:
        yield from self._replay(self._risk_blocks_path, PaperRiskBlockRecord)

    def _replay(self, path: Path, model_cls: type[T]) -> Iterator[T]:
        """Yield records from ``path``; skip-and-warn on malformed lines.

        Counter increments on both success (``n_records_loaded``) and
        failure (``n_parse_errors``) — see module docstring.
        """
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                stripped = raw.strip()
                if not stripped:
                    # Blank lines are silently skipped — neither success
                    # nor parse error.  An append followed by abrupt
                    # truncation can leave a trailing empty line.
                    continue
                try:
                    record = model_cls.model_validate_json(stripped)
                except Exception as exc:
                    self._n_parse_errors += 1
                    logger.warning(
                        "paper_ledger.parse_error",
                        path=str(path),
                        lineno=lineno,
                        error=str(exc),
                        payload_preview=stripped[:200],
                    )
                    continue
                self._n_records_loaded += 1
                yield record

    # ── Health / introspection ────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Return reader-side counters and file paths for operator sanity-checks.

        ``n_records_loaded`` / ``n_parse_errors`` accumulate across all
        replay calls on this instance — they are NOT per-file.  Compare
        ``n_records_loaded + n_parse_errors`` against the raw non-blank
        line count of the three files to verify the reader saw everything.
        """
        return {
            "n_records_loaded": self._n_records_loaded,
            "n_parse_errors": self._n_parse_errors,
            "orders_path": str(self._orders_path),
            "fills_path": str(self._fills_path),
            "settlements_path": str(self._settlements_path),
            "rejections_path": str(self._rejections_path),
        }

    @property
    def base_dir(self) -> Path:
        return self._base_dir
