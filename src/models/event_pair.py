from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from src.models.market import Market


class MatchStatus(StrEnum):
    """Whether the pair has been evaluated and what the outcome was."""

    PENDING = "pending"       # Not yet evaluated
    MATCHED = "matched"       # Confirmed equivalent — eligible for edge calc
    REJECTED = "rejected"     # Failed one or more equivalence rules


class MatchConfidence(StrEnum):
    """
    Confidence level assigned by the matcher.
    Only EXACT is tradeable. All others must be rejected.
    See PRODUCT_SPEC.md Section 5.
    """

    EXACT = "exact"           # Normalized strings match + deadline + resolver align
    PROBABLE = "probable"     # Match after synonym expansion; deadline/resolver uncertain
    POSSIBLE = "possible"     # Partial keyword overlap
    UNKNOWN = "unknown"       # No meaningful overlap


# Reason codes mirror PRODUCT_SPEC.md Section 4 and Section 7.
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

    After matching, status is either MATCHED (proceed to edge calc)
    or REJECTED (log reason, discard).
    """

    polymarket: Market
    kalshi: Market

    # Matching results
    status: MatchStatus = Field(default=MatchStatus.PENDING)
    confidence: MatchConfidence = Field(default=MatchConfidence.UNKNOWN)

    # Set when the YES/NO directions are inverted between venues.
    # When True, the executor must flip the Kalshi leg (buy YES instead of NO, or vice versa).
    directions_inverted: bool = Field(
        default=False,
        description="True when Polymarket YES and Kalshi YES refer to opposite outcomes",
    )

    # Populated when status == REJECTED
    reject_reason: str | None = Field(
        default=None,
        description="One of REJECT_REASON_CODES; None when status is not REJECTED",
    )
    reject_detail: str | None = Field(
        default=None,
        description="Human-readable elaboration for logs; not used in business logic",
    )

    @property
    def is_tradeable(self) -> bool:
        return self.status == MatchStatus.MATCHED and self.confidence == MatchConfidence.EXACT

    @property
    def pair_key(self) -> str:
        """Stable string key for this pair, used in approved_pairs lookups and logs."""
        return f"{self.polymarket.venue_market_id}::{self.kalshi.venue_market_id}"

    model_config = {"frozen": True}
