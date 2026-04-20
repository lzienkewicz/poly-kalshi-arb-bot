from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.models.market import Side
from src.models.opportunity import Direction


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class PositionStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    VOIDED = "voided"


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    ts_entered: datetime
    ts_settled: datetime | None = None
    mode: TradingMode
    question: str
    poly_market_id: str
    kalshi_market_id: str
    direction: Direction
    poly_side: Side
    kalshi_side: Side
    poly_price: float = Field(ge=0.0, le=1.0)
    kalshi_price: float = Field(ge=0.0, le=1.0)
    trade_size_usdc: float = Field(gt=0.0)
    net_edge_at_entry: float
    pnl: float | None = None
    status: PositionStatus = PositionStatus.OPEN
