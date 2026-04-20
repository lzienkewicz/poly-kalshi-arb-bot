from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from src.models.event_pair import EventPair
from src.models.orderbook import OrderBook


class TradeDirection(StrEnum):
    A = "A"  # Buy YES on Polymarket, Buy NO on Kalshi
    B = "B"  # Buy NO on Polymarket, Buy YES on Kalshi


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

    pair: EventPair
    poly_book: OrderBook
    kalshi_book: OrderBook
    evaluated_at: datetime

    direction: TradeDirection
    poly_ask: float = Field(ge=0.0, le=1.0)
    kalshi_ask: float = Field(ge=0.0, le=1.0)

    gross_edge: float
    fee_estimate: float = Field(ge=0.0)
    slippage_buffer: float = Field(ge=0.0)
    stale_penalty: float = Field(ge=0.0)
    net_edge: float

    poly_book_stale: bool = Field(default=False)
    kalshi_book_stale: bool = Field(default=False)

    executable_size_usdc: float = Field(gt=0.0)

    status: OpportunityStatus
    fail_reason: str | None = Field(default=None)
    fail_detail: str | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_net_edge_formula(self) -> "Opportunity":
        expected = round(
            self.gross_edge - self.fee_estimate - self.slippage_buffer - self.stale_penalty, 8
        )
        if abs(self.net_edge - expected) > 1e-6:
            raise ValueError(
                f"net_edge {self.net_edge} does not match formula result {expected}. "
                "Check edge calculator."
            )
        return self

    @property
    def estimated_profit_usdc(self) -> float:
        return self.net_edge * self.executable_size_usdc

    @property
    def poly_side(self) -> str:
        return "YES" if self.direction == TradeDirection.A else "NO"

    @property
    def kalshi_side(self) -> str:
        return "NO" if self.direction == TradeDirection.A else "YES"

    model_config = {"frozen": True}
