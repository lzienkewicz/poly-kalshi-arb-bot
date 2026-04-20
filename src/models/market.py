from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, computed_field


class Venue(StrEnum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class Side(StrEnum):
    YES = "YES"
    NO = "NO"

    def opposite(self) -> Side:
        return Side.NO if self == Side.YES else Side.YES


class MarketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    RESOLVED = "resolved"


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    venue: Venue
    question: str
    close_time: datetime
    status: MarketStatus
    resolver: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def question_normalized(self) -> str:
        return re.sub(r"[^\w\s]", "", self.question.lower()).strip()

    @property
    def is_open(self) -> bool:
        return self.status == MarketStatus.OPEN

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.close_time
