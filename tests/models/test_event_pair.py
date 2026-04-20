from __future__ import annotations

import pytest

from src.models.event_pair import (
    REJECT_REASON_CODES,
    EventPair,
    MatchConfidence,
    MatchStatus,
)
from src.models.market import Market


def test_event_pair_defaults(poly_market: Market, kalshi_market: Market):
    pair = EventPair(polymarket=poly_market, kalshi=kalshi_market)
    assert pair.status == MatchStatus.PENDING
    assert pair.confidence == MatchConfidence.UNKNOWN
    assert pair.directions_inverted is False
    assert pair.reject_reason is None


def test_pair_key_format(poly_market: Market, kalshi_market: Market):
    pair = EventPair(polymarket=poly_market, kalshi=kalshi_market)
    assert pair.pair_key == "cond_abc123::FED-JUNE-CUT"


def test_is_tradeable_requires_matched_and_exact(poly_market: Market, kalshi_market: Market):
    pending = EventPair(polymarket=poly_market, kalshi=kalshi_market)
    assert pending.is_tradeable is False

    matched_probable = EventPair(
        polymarket=poly_market,
        kalshi=kalshi_market,
        status=MatchStatus.MATCHED,
        confidence=MatchConfidence.PROBABLE,
    )
    assert matched_probable.is_tradeable is False

    matched_exact = EventPair(
        polymarket=poly_market,
        kalshi=kalshi_market,
        status=MatchStatus.MATCHED,
        confidence=MatchConfidence.EXACT,
    )
    assert matched_exact.is_tradeable is True


def test_rejected_pair(poly_market: Market, kalshi_market: Market):
    pair = EventPair(
        polymarket=poly_market,
        kalshi=kalshi_market,
        status=MatchStatus.REJECTED,
        reject_reason="EQUIV_DEADLINE_GAP",
        reject_detail="Deadlines differ by 36 hours",
    )
    assert pair.is_tradeable is False
    assert pair.reject_reason == "EQUIV_DEADLINE_GAP"


def test_directions_inverted(poly_market: Market, kalshi_market: Market):
    pair = EventPair(
        polymarket=poly_market,
        kalshi=kalshi_market,
        directions_inverted=True,
    )
    assert pair.directions_inverted is True


def test_reject_reason_codes_are_complete():
    expected = {
        "EQUIV_EVENT_MISMATCH",
        "EQUIV_DIRECTION_MISMATCH",
        "EQUIV_DEADLINE_GAP",
        "EQUIV_RESOLVER_MISMATCH",
        "EQUIV_CONDITIONAL_MISMATCH",
        "EQUIV_CONFIDENCE_NOT_EXACT",
        "EQUIV_NOT_IN_APPROVED_PAIRS",
    }
    assert REJECT_REASON_CODES == expected


def test_event_pair_is_frozen(poly_market: Market, kalshi_market: Market):
    pair = EventPair(polymarket=poly_market, kalshi=kalshi_market)
    with pytest.raises(Exception):
        pair.status = MatchStatus.MATCHED  # type: ignore[misc]
