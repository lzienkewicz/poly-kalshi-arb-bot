from __future__ import annotations

from datetime import datetime
from enum import StrEnum

<<<<<<< HEAD
from pydantic import BaseModel, ConfigDict, Field

from src.models.event_pair import EventPair
from src.models.market import Side
from src.models.orderbook import OrderBook


class Direction(StrEnum):
    A = "A"  # Buy YES on Polymarket, Buy NO on Kalshi
    B = "B"  # Buy NO on Polymarket, Buy YES on Kalshi


class OpportunityClassification(StrEnum):
    TRADEABLE = "TRADEABLE"
    REJECTED_EDGE = "REJECTED_EDGE"
    REJECTED_EQUIV = "REJECTED_EQUIV"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_DEPTH = "REJECTED_DEPTH"
    REJECTED_RISK = "REJECTED_RISK"


class ReasonCode(StrEnum):
    TRADEABLE = "TRADEABLE"
    REJECTED_EDGE = "REJECTED_EDGE"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_DEPTH = "REJECTED_DEPTH"
    REJECTED_RISK = "REJECTED_RISK"
    EQUIV_EVENT_MISMATCH = "EQUIV_EVENT_MISMATCH"
    EQUIV_DIRECTION_MISMATCH = "EQUIV_DIRECTION_MISMATCH"
    EQUIV_DEADLINE_GAP = "EQUIV_DEADLINE_GAP"
    EQUIV_RESOLVER_MISMATCH = "EQUIV_RESOLVER_MISMATCH"
    EQUIV_CONDITIONAL_MISMATCH = "EQUIV_CONDITIONAL_MISMATCH"
    EQUIV_CONFIDENCE_NOT_EXACT = "EQUIV_CONFIDENCE_NOT_EXACT"


class Opportunity(BaseModel):
    model_config = ConfigDict(frozen=True)

    pair: EventPair
    poly_book: OrderBook
    kalshi_book: OrderBook
    direction: Direction
    poly_side: Side
    kalshi_side: Side
    poly_ask: float = Field(ge=0.0, le=1.0)
    kalshi_ask: float = Field(ge=0.0, le=1.0)
    gross_edge: float
    stale_legs: int = Field(ge=0, le=2)
    stale_penalty_total: float = Field(ge=0.0)
    net_edge: float
    classification: OpportunityClassification
    reason_code: ReasonCode
    evaluated_at: datetime
=======
from pydantic import BaseModel, Field, model_validator

from src.models.event_pair import EventPair
from src.models.orderbook import OrderBook


class TradeDirection(StrEnum):
    """
    Which leg is on which side.
    See PRODUCT_SPEC.md Section 6.2 for formula definitions.

    A: Buy YES on Polymarket, Buy NO on Kalshi
    B: Buy NO on Polymarket, Buy YES on Kalshi
    """

    A = "A"
    B = "B"


class OpportunityStatus(StrEnum):
    TRADEABLE = "TRADEABLE"
    REJECTED_EDGE = "REJECTED_EDGE"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_DEPTH = "REJECTED_DEPTH"
    REJECTED_RISK = "REJECTED_RISK"
    REJECTED_EQUIV = "REJECTED_EQUIV"


class Opportunity(BaseModel):
    """
    The result of running the edge calculator against a confirmed EventPair.

    All monetary values are in USD [0, 1] per contract unless noted.
    See PRODUCT_SPEC.md Sections 6–8 for formula details.
    """

    # Source data
    pair: EventPair
    poly_book: OrderBook = Field(description="Polymarket order book snapshot used for this calc")
    kalshi_book: OrderBook = Field(description="Kalshi order book snapshot used for this calc")
    evaluated_at: datetime = Field(description="UTC timestamp when edge was calculated")

    # Best direction found
    direction: TradeDirection = Field(description="A or B — whichever produced higher gross edge")

    # Price inputs (the actual ask prices used)
    poly_ask: float = Field(ge=0.0, le=1.0, description="Polymarket ask used (YES or NO depending on direction)")
    kalshi_ask: float = Field(ge=0.0, le=1.0, description="Kalshi ask used (NO or YES depending on direction)")

    # Edge decomposition — all in USD per contract
    gross_edge: float = Field(description="1.00 - (poly_ask + kalshi_ask) before any adjustments")
    fee_estimate: float = Field(ge=0.0, description="Combined fee cost for both legs (poly_fee + kalshi_fee)")
    slippage_buffer: float = Field(ge=0.0, description="Combined slippage allowance for both legs")
    stale_penalty: float = Field(ge=0.0, description="Penalty applied for stale price data (0 if both fresh)")
    net_edge: float = Field(description="gross_edge - fee_estimate - slippage_buffer - stale_penalty")

    # Stale flags
    poly_book_stale: bool = Field(default=False)
    kalshi_book_stale: bool = Field(default=False)

    # Sizing
    executable_size_usdc: float = Field(
        gt=0.0,
        description=(
            "Max notional (in USDC) that can be filled at top-of-book on both sides. "
            "Capped by config TRADE_SIZE_USDC and available depth."
        ),
    )

    # Outcome
    status: OpportunityStatus
    fail_reason: str | None = Field(
        default=None,
        description="Reason code when status is not TRADEABLE; None otherwise",
    )
    fail_detail: str | None = Field(
        default=None,
        description="Human-readable elaboration for logs",
    )

    @model_validator(mode="after")
    def _validate_net_edge_formula(self) -> "Opportunity":
        expected = round(self.gross_edge - self.fee_estimate - self.slippage_buffer - self.stale_penalty, 8)
        if abs(self.net_edge - expected) > 1e-6:
            raise ValueError(
                f"net_edge {self.net_edge} does not match formula result {expected}. "
                "Check edge calculator."
            )
        return self

    @property
    def estimated_profit_usdc(self) -> float:
        """Expected profit in USDC for executing this opportunity at executable_size_usdc."""
        return self.net_edge * self.executable_size_usdc

    @property
    def poly_side(self) -> str:
        return "YES" if self.direction == TradeDirection.A else "NO"

    @property
    def kalshi_side(self) -> str:
        return "NO" if self.direction == TradeDirection.A else "YES"

    model_config = {"frozen": True}
>>>>>>> 3eb221c205617b7a73ae5d47876a82b95b7391d1
