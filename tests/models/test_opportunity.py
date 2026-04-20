from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.models.event_pair import EventPair, MatchConfidence
from src.models.market import Market, Side
from src.models.opportunity import (
    Direction,
    Opportunity,
    OpportunityClassification,
    ReasonCode,
)
from src.models.orderbook import OrderBook


def _make_opportunity(
    poly_market: Market,
    kalshi_market: Market,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
    *,
    net_edge: float = 0.03,
    classification: OpportunityClassification = OpportunityClassification.TRADEABLE,
    reason_code: ReasonCode = ReasonCode.TRADEABLE,
    stale_legs: int = 0,
) -> Opportunity:
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.EXACT,
    )
    return Opportunity(
        pair=pair,
        poly_book=poly_book,
        kalshi_book=kalshi_book,
        direction=Direction.A,
        poly_side=Side.YES,
        kalshi_side=Side.NO,
        poly_ask=0.55,
        kalshi_ask=0.45,
        gross_edge=0.00 if stale_legs == 0 else net_edge + 0.005 * stale_legs,
        stale_legs=stale_legs,
        stale_penalty_total=stale_legs * 0.005,
        net_edge=net_edge,
        classification=classification,
        reason_code=reason_code,
        evaluated_at=datetime.now(timezone.utc),
    )


def test_opportunity_creates(
    poly_market: Market,
    kalshi_market: Market,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
):
    opp = _make_opportunity(poly_market, kalshi_market, poly_book, kalshi_book)
    assert opp.direction == Direction.A
    assert opp.poly_side == Side.YES
    assert opp.kalshi_side == Side.NO
    assert opp.classification == OpportunityClassification.TRADEABLE
    assert opp.reason_code == ReasonCode.TRADEABLE


def test_ask_price_bounds(
    poly_market: Market,
    kalshi_market: Market,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
):
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.EXACT,
    )
    with pytest.raises(ValidationError):
        Opportunity(
            pair=pair,
            poly_book=poly_book,
            kalshi_book=kalshi_book,
            direction=Direction.A,
            poly_side=Side.YES,
            kalshi_side=Side.NO,
            poly_ask=1.01,  # out of bounds
            kalshi_ask=0.45,
            gross_edge=0.03,
            stale_legs=0,
            stale_penalty_total=0.0,
            net_edge=0.03,
            classification=OpportunityClassification.TRADEABLE,
            reason_code=ReasonCode.TRADEABLE,
            evaluated_at=datetime.now(timezone.utc),
        )


def test_stale_legs_bounds(
    poly_market: Market,
    kalshi_market: Market,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
):
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.EXACT,
    )
    with pytest.raises(ValidationError):
        Opportunity(
            pair=pair,
            poly_book=poly_book,
            kalshi_book=kalshi_book,
            direction=Direction.B,
            poly_side=Side.NO,
            kalshi_side=Side.YES,
            poly_ask=0.45,
            kalshi_ask=0.55,
            gross_edge=0.00,
            stale_legs=3,  # max is 2
            stale_penalty_total=0.0,
            net_edge=-0.015,
            classification=OpportunityClassification.REJECTED_STALE,
            reason_code=ReasonCode.REJECTED_STALE,
            evaluated_at=datetime.now(timezone.utc),
        )


def test_rejected_equiv_reason_code(
    poly_market: Market,
    kalshi_market: Market,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
):
    opp = _make_opportunity(
        poly_market,
        kalshi_market,
        poly_book,
        kalshi_book,
        net_edge=-0.1,
        classification=OpportunityClassification.REJECTED_EQUIV,
        reason_code=ReasonCode.EQUIV_DEADLINE_GAP,
    )
    assert opp.classification == OpportunityClassification.REJECTED_EQUIV
    assert opp.reason_code == ReasonCode.EQUIV_DEADLINE_GAP


def test_all_reason_codes_are_valid():
    for code in ReasonCode:
        assert isinstance(code.value, str)


def test_opportunity_is_frozen(
    poly_market: Market,
    kalshi_market: Market,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
):
    opp = _make_opportunity(poly_market, kalshi_market, poly_book, kalshi_book)
    with pytest.raises(Exception):
        opp.net_edge = 0.99  # type: ignore[misc]
