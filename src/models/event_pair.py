from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, computed_field

from src.models.market import Market


class MatchConfidence(StrEnum):
    EXACT = "EXACT"
    PROBABLE = "PROBABLE"
    POSSIBLE = "POSSIBLE"
    UNKNOWN = "UNKNOWN"


class EventPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    poly_market: Market
    kalshi_market: Market
    confidence: MatchConfidence
    direction_inverted: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pair_id(self) -> str:
        return f"{self.poly_market.id}:{self.kalshi_market.id}"
