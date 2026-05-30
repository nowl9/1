"""Shared pydantic data models used across all layers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class DataSource(str, Enum):
    DERIBIT = "deribit"
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


# ── Layer 1: raw feed models ──────────────────────────────────────────────────


class Greeks(BaseModel):
    """Option Greeks as reported by Deribit."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float = 0.0


class OptionTick(BaseModel):
    """A single tick from the Deribit options feed.

    Instrument name follows Deribit convention: BTC-DDMMMYY-STRIKE-C/P
    e.g.  BTC-26APR24-50000-C
    """

    instrument_name: str
    strike: float = Field(gt=0)
    expiry: datetime  # UTC
    option_type: OptionType

    # Prices are expressed as a fraction of underlying (Deribit convention)
    bid: float | None = None
    ask: float | None = None
    mark_price: float = Field(ge=0)

    # Implied vol in percent (e.g. 65.0 = 65%)
    bid_iv: float | None = None
    ask_iv: float | None = None
    mark_iv: float | None = None

    greeks: Greeks | None = None
    underlying_price: float = Field(gt=0)
    index_price: float = Field(gt=0)

    # Open interest in contracts
    open_interest: float = 0.0

    timestamp: datetime  # UTC, millisecond precision

    @property
    def bid_price_usd(self) -> float | None:
        """Bid price in USD (bid fraction × underlying)."""
        return self.bid * self.underlying_price if self.bid is not None else None

    @property
    def ask_price_usd(self) -> float | None:
        """Ask price in USD."""
        return self.ask * self.underlying_price if self.ask is not None else None

    @property
    def mid_price(self) -> float | None:
        """Mid price as fraction of underlying."""
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return self.ask - self.bid
        return None


class PredictionMarketTick(BaseModel):
    """A normalised tick from Polymarket or Kalshi.

    YES price is the probability of the event [0, 1].
    """

    source: DataSource
    contract_id: str
    question: str  # human-readable, e.g. "BTC above $100k on June 30?"

    # Threshold and expiry decoded from contract metadata
    strike: float | None = None  # USD threshold, if parseable
    expiry: datetime | None = None  # contract resolution time, UTC

    yes_bid: float | None = None  # [0, 1]
    yes_ask: float | None = None  # [0, 1]
    no_bid: float | None = None
    no_ask: float | None = None

    # Order book depth levels for fill-adjusted edge calculation.
    # Each entry is (price [0,1], size_usd). Sorted price ascending for YES.
    order_book_yes: list[tuple[float, float]] = Field(default_factory=list)
    order_book_no: list[tuple[float, float]] = Field(default_factory=list)

    # Product semantics parsed from venue metadata at normalization time.
    # direction:    YES-leg polarity ("above" | "below").
    # product_type: "terminal" European digital, or "one_touch" barrier.
    direction: Literal["above", "below"] = "above"
    product_type: Literal["terminal", "one_touch"] = "terminal"

    timestamp: datetime

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return None

    @property
    def no_mid(self) -> float | None:
        if self.no_bid is not None and self.no_ask is not None:
            return (self.no_bid + self.no_ask) / 2
        return None


# ── Layer 2: pricing models ───────────────────────────────────────────────────


class ProbabilityQuote(BaseModel):
    """Canonical probability representation used by the pricing engine and
    signal generator.

    bid_prob: lower bound on the market-implied probability (from bid side)
    ask_prob: upper bound (from ask side)
    mid_prob: best estimate

    For Deribit-derived quotes this is the digital option probability.
    For prediction markets it is simply the YES price.
    """

    source: DataSource
    contract_id: str  # instrument name or market id

    strike: float = Field(gt=0)
    expiry: datetime

    bid_prob: float = Field(ge=0.0, le=1.0)
    ask_prob: float = Field(ge=0.0, le=1.0)
    mid_prob: float = Field(ge=0.0, le=1.0)

    # Whether this is probability of BTC being *above* the strike
    direction: Literal["above", "below"] = "above"

    # Path-dependent products (one-touch barriers) are mispriced by the
    # terminal digital pricer; tagged here so the signal filter can skip them.
    product_type: Literal["terminal", "one_touch"] = "terminal"

    # Settlement mechanism matters for basis adjustment
    settlement_type: Literal["deribit_twap", "kalshi_rti", "polymarket_spot", "unknown"] = (
        "unknown"
    )

    # Order-book depth carried from the source tick for the depth gate.
    # None = depth unknown (gate skipped); [] = known-empty (gate filters).
    order_book_yes: list[tuple[float, float]] | None = None
    order_book_no: list[tuple[float, float]] | None = None

    timestamp: datetime

    @field_validator("ask_prob")
    @classmethod
    def ask_gte_bid(cls, v: float, info: object) -> float:
        # Soft check only — markets can be crossed temporarily
        return v


# ── Layer 3: signal models ────────────────────────────────────────────────────


class ArbitrageSignal(BaseModel):
    """A detected edge between options-implied probability and a prediction
    market price."""

    options_quote: ProbabilityQuote   # from Deribit
    pm_quote: ProbabilityQuote        # from Polymarket or Kalshi

    # Positive means prediction market is *too high* → sell YES (or buy NO)
    # Negative means prediction market is *too low* → buy YES
    raw_edge: float
    adjusted_edge: float  # after settlement basis correction (conservative)

    # Fill-adjusted edge: accounts for order book depth when walking the book.
    # None when no order book data is available (falls back to adjusted_edge).
    fill_adjusted_edge: float | None = None

    # Side to trade on the prediction market
    trade_side: Literal["buy_yes", "sell_yes", "buy_no", "sell_no"]

    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    # Feed staleness at signal generation time (ms per source).
    # Used for monitoring and the data freshness gate.
    feed_staleness_ms: dict[str, float] = Field(default_factory=dict)

    # Volatility regime at signal generation time
    vol_regime: str = "normal"

    timestamp: datetime
