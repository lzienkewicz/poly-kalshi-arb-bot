from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from src.models.market import Venue


class PriceLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(ge=0.0)


class OrderBook(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: Venue
    market_id: str
    yes_ask: PriceLevel
    no_ask: PriceLevel
    yes_bid: PriceLevel | None = None
    no_bid: PriceLevel | None = None
    fetched_at: datetime

    def is_stale(self, max_age_s: int) -> bool:
        age = (datetime.now(timezone.utc) - self.fetched_at).total_seconds()
        return age > max_age_s
