from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.models.event_pair import EventPair
from src.models.opportunity import Opportunity, OpportunityStatus, TradeDirection
from src.models.orderbook import OrderBook


def _make_opportunity(
    matched_pair: EventPair,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
    *,
    gross_edge: float = 0.00,
    fee_estimate: float = 0.004,
    slippage_buffer: float = 0.010,
    stale_penalty: float = 0.0,
    status: OpportunityStatus = OpportunityStatus.TRADEABLE,
    poly_ask: float = 0.55,
    kalshi_ask: float = 0.45,
) -> Opportunity:
    net = round(gross_edge - fee_estimate - slippage_buffer - stale_penalty, 8)
    return Opportunity(
        pair=matched_pair,
        poly_book=poly_book,
        kalshi_book=kalshi_book,
        evaluated_at=datetime.now(timezone.utc),
        direction=TradeDirection.A,
        poly_ask=poly_ask,
        kalshi_ask=kalshi_ask,
        gross_edge=gross_edge,
        fee_estimate=fee_estimate,
        slippage_buffer=slippage_buffer,
        stale_penalty=stale_penalty,
        net_edge=net,
        executable_size_usdc=10.0,
        status=status,
    )


def test_opportunity_creates(tradeable_opportunity: Opportunity):
    assert tradeable_opportunity.direction == TradeDirection.A
    assert tradeable_opportunity.status == OpportunityStatus.TRADEABLE
    assert tradeable_opportunity.poly_side == "YES"
    assert tradeable_opportunity.kalshi_side == "NO"


def test_direction_b_sides(
    matched_pair: EventPair, poly_book: OrderBook, kalshi_book: OrderBook
):
    gross = 1.0 - (0.46 + 0.54)
    fee, slip, stale = 0.004, 0.010, 0.0
    net = round(gross - fee - slip - stale, 8)
    opp = Opportunity(
        pair=matched_pair,
        poly_book=poly_book,
        kalshi_book=kalshi_book,
        evaluated_at=datetime.now(timezone.utc),
        direction=TradeDirection.B,
        poly_ask=0.46,
        kalshi_ask=0.54,
        gross_edge=gross,
        fee_estimate=fee,
        slippage_buffer=slip,
        stale_penalty=stale,
        net_edge=net,
        executable_size_usdc=10.0,
        status=OpportunityStatus.TRADEABLE,
    )
    assert opp.poly_side == "NO"
    assert opp.kalshi_side == "YES"


def test_net_edge_formula_enforced(
    matched_pair: EventPair, poly_book: OrderBook, kalshi_book: OrderBook
):
    with pytest.raises(ValidationError, match="net_edge"):
        Opportunity(
            pair=matched_pair,
            poly_book=poly_book,
            kalshi_book=kalshi_book,
            evaluated_at=datetime.now(timezone.utc),
            direction=TradeDirection.A,
            poly_ask=0.55,
            kalshi_ask=0.45,
            gross_edge=0.00,
            fee_estimate=0.004,
            slippage_buffer=0.010,
            stale_penalty=0.0,
            net_edge=0.99,  # wrong
            executable_size_usdc=10.0,
            status=OpportunityStatus.TRADEABLE,
        )


def test_ask_price_bounds(
    matched_pair: EventPair, poly_book: OrderBook, kalshi_book: OrderBook
):
    with pytest.raises(ValidationError):
        _make_opportunity(
            matched_pair, poly_book, kalshi_book, poly_ask=1.01
        )


def test_estimated_profit(tradeable_opportunity: Opportunity):
    expected = tradeable_opportunity.net_edge * tradeable_opportunity.executable_size_usdc
    assert tradeable_opportunity.estimated_profit_usdc == pytest.approx(expected)


def test_stale_flags_default_false(tradeable_opportunity: Opportunity):
    assert tradeable_opportunity.poly_book_stale is False
    assert tradeable_opportunity.kalshi_book_stale is False


def test_rejected_opportunity(
    matched_pair: EventPair, poly_book: OrderBook, kalshi_book: OrderBook
):
    opp = _make_opportunity(
        matched_pair, poly_book, kalshi_book, status=OpportunityStatus.REJECTED_EDGE
    )
    assert opp.status == OpportunityStatus.REJECTED_EDGE


def test_opportunity_is_frozen(tradeable_opportunity: Opportunity):
    with pytest.raises(Exception):
        tradeable_opportunity.net_edge = 0.99  # type: ignore[misc]
