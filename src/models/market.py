from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Venue(StrEnum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class Market(BaseModel):
    """Normalized representation of a binary prediction market on any supported venue."""

    venue: Venue
    venue_market_id: str = Field(description="Venue-native unique identifier (conditionId or ticker)")
    question: str = Field(description="Human-readable resolution question, as returned by the venue")
    question_normalized: str = Field(description="Lowercased, punctuation-stripped question used for matching")

    outcome_yes: str = Field(default="Yes")
    outcome_no: str = Field(default="No")

    resolution_source: str | None = Field(default=None)
    category: str | None = Field(default=None)

    close_time: datetime = Field(description="When the market stops accepting new orders (UTC)")
    resolution_time: datetime | None = Field(default=None)

    is_open: bool = Field(default=True)

    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("question_normalized", mode="before")
    @classmethod
    def _normalize(cls, v: str, info: Any) -> str:
        if v:
            return v
        question: str = info.data.get("question", "")
        return _normalize_question(question)

    model_config = {"frozen": True}


def _normalize_question(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
