from __future__ import annotations

<<<<<<< HEAD
import re
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, computed_field
=======
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator
>>>>>>> 3eb221c205617b7a73ae5d47876a82b95b7391d1


class Venue(StrEnum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


<<<<<<< HEAD
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
=======
class Market(BaseModel):
    """Normalized representation of a binary prediction market on any supported venue."""

    # Identity
    venue: Venue
    venue_market_id: str = Field(description="Venue-native unique identifier (conditionId or ticker)")
    question: str = Field(description="Human-readable resolution question, as returned by the venue")
    question_normalized: str = Field(description="Lowercased, punctuation-stripped question used for matching")

    # Outcome labels
    # Binary markets always have exactly two outcomes.
    # outcome_yes is the condition under which the YES token pays $1.
    outcome_yes: str = Field(default="Yes", description="Label for the YES outcome")
    outcome_no: str = Field(default="No", description="Label for the NO outcome")

    # Resolution metadata
    resolution_source: str | None = Field(
        default=None,
        description="Authority or data source that determines settlement (e.g. 'AP', 'CFTC', 'official results')",
    )
    category: str | None = Field(default=None, description="Market category as returned by the venue")

    # Timing
    close_time: datetime = Field(description="When the market stops accepting new orders (UTC)")
    resolution_time: datetime | None = Field(
        default=None,
        description="When the market is expected to resolve; may differ from close_time (UTC)",
    )

    # Status — only open markets should be held in memory
    is_open: bool = Field(default=True)

    # Raw payload preserved for debugging and audit
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Original API response payload; not used in any business logic",
    )

    @field_validator("question_normalized", mode="before")
    @classmethod
    def _normalize(cls, v: str, info: Any) -> str:
        """If the caller did not provide a pre-normalized string, derive it from question."""
        if v:
            return v
        question: str = info.data.get("question", "")
        return _normalize_question(question)

    model_config = {"frozen": True}


def _normalize_question(text: str) -> str:
    """Lowercase and strip punctuation for deterministic matching."""
    import re

    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
>>>>>>> 3eb221c205617b7a73ae5d47876a82b95b7391d1
