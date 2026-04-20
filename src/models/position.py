from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from src.models.opportunity import Opportunity, TradeDirection


class PositionStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    VOIDED = "voided"


class PositionLeg(BaseModel):
    """One executed leg of a two-legged arbitrage position."""

    venue: str = Field(description="'polymarket' or 'kalshi'")
    venue_market_id: str
    side: str = Field(description="'YES' or 'NO'")
    fill_price: float = Field(ge=0.0, le=1.0)
    contracts: float = Field(gt=0.0)
    notional_usdc: float = Field(gt=0.0)
    fee_paid_usdc: float = Field(ge=0.0)
    is_simulated: bool = Field(default=True)

    model_config = {"frozen": True}


class Position(BaseModel):
    """A two-legged arbitrage position opened from a single Opportunity."""

    position_id: str
    mode: str = Field(description="'paper' or 'live'")

    opportunity: Opportunity

    poly_leg: PositionLeg
    kalshi_leg: PositionLeg
    direction: TradeDirection

    opened_at: datetime
    settled_at: datetime | None = Field(default=None)

    status: PositionStatus = Field(default=PositionStatus.OPEN)
    winning_side: str | None = Field(default=None)

    gross_pnl_usdc: float | None = Field(default=None)
    net_pnl_usdc: float | None = Field(default=None)

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
