from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from src.models.market import Market


class MatchStatus(StrEnum):
    PENDING = "pending"
    MATCHED = "matched"
    REJECTED = "rejected"


class MatchConfidence(StrEnum):
    EXACT = "exact"
    PROBABLE = "probable"
    POSSIBLE = "possible"
    UNKNOWN = "unknown"


REJECT_REASON_CODES = frozenset(
    {
        "EQUIV_EVENT_MISMATCH",
        "EQUIV_DIRECTION_MISMATCH",
        "EQUIV_DEADLINE_GAP",
        "EQUIV_RESOLVER_MISMATCH",
        "EQUIV_CONDITIONAL_MISMATCH",
        "EQUIV_CONFIDENCE_NOT_EXACT",
        "EQUIV_NOT_IN_APPROVED_PAIRS",
    }
)


class EventPair(BaseModel):
    """
    A candidate pairing of one Polymarket market and one Kalshi market
    that potentially represent the same real-world event.
    """

    polymarket: Market
    kalshi: Market

    status: MatchStatus = Field(default=MatchStatus.PENDING)
    confidence: MatchConfidence = Field(default=MatchConfidence.UNKNOWN)

    directions_inverted: bool = Field(default=False)

    reject_reason: str | None = Field(default=None)
    reject_detail: str | None = Field(default=None)

    @property
    def is_tradeable(self) -> bool:
        return self.status == MatchStatus.MATCHED and self.confidence == MatchConfidence.EXACT

    @property
    def pair_key(self) -> str:
        return f"{self.polymarket.venue_market_id}::{self.kalshi.venue_market_id}"

    model_config = {"frozen": True}
