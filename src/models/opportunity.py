from __future__ import annotations

from datetime import datetime
from enum import StrEnum

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
