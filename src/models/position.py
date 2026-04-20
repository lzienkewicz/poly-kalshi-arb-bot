from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from src.models.opportunity import Opportunity, TradeDirection


class PositionStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    VOIDED = "voided"   # Market cancelled or resolved ambiguously


class PositionLeg(BaseModel):
    """One executed leg of a two-legged arbitrage position."""

    venue: str = Field(description="'polymarket' or 'kalshi'")
    venue_market_id: str
    side: str = Field(description="'YES' or 'NO'")
    fill_price: float = Field(ge=0.0, le=1.0, description="Actual or simulated fill price in [0, 1]")
    contracts: float = Field(gt=0.0, description="Number of contracts filled")
    notional_usdc: float = Field(gt=0.0, description="fill_price * contracts in USDC")
    fee_paid_usdc: float = Field(ge=0.0)
    is_simulated: bool = Field(default=True, description="False only when a real order was placed")

    model_config = {"frozen": True}


class Position(BaseModel):
    """
    A two-legged arbitrage position opened from a single Opportunity.

    In paper mode, both legs are always simulated.
    In live mode, legs are real fills.
    """

    # Identity
    position_id: str = Field(description="UUID assigned at creation time")
    mode: str = Field(description="'paper' or 'live'")

    # Source
    opportunity: Opportunity

    # Legs
    poly_leg: PositionLeg
    kalshi_leg: PositionLeg
    direction: TradeDirection

    # Timing
    opened_at: datetime = Field(description="UTC timestamp when both legs were filled/simulated")
    settled_at: datetime | None = Field(default=None)

    # Resolution
    status: PositionStatus = Field(default=PositionStatus.OPEN)
    winning_side: str | None = Field(
        default=None,
        description="'YES' or 'NO' — which outcome resolved True. Set on settlement.",
    )

    # P&L — populated on settlement
    gross_pnl_usdc: float | None = Field(
        default=None,
        description="Settlement proceeds minus total cost of both legs, before fees",
    )
    net_pnl_usdc: float | None = Field(
        default=None,
        description="gross_pnl_usdc minus total fees paid across both legs",
    )

    @property
    def total_cost_usdc(self) -> float:
        return self.poly_leg.notional_usdc + self.kalshi_leg.notional_usdc

    @property
    def total_fees_usdc(self) -> float:
        return self.poly_leg.fee_paid_usdc + self.kalshi_leg.fee_paid_usdc

    @property
    def is_settled(self) -> bool:
        return self.status == PositionStatus.SETTLED

    model_config = {"frozen": True}
