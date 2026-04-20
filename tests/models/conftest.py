from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.models.event_pair import EventPair, MatchConfidence, MatchStatus
from src.models.market import Market, Venue
from src.models.opportunity import Opportunity, OpportunityStatus, TradeDirection
from src.models.orderbook import BookLevel, BookSide, OrderBook
from src.models.position import PositionLeg


@pytest.fixture()
def future_time() -> datetime:
    return datetime(2099, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def past_time() -> datetime:
    return datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def poly_market(future_time: datetime) -> Market:
    return Market(
        venue=Venue.POLYMARKET,
        venue_market_id="cond_abc123",
        question="Will the Fed cut rates in June?",
        question_normalized="",
        close_time=future_time,
    )


@pytest.fixture()
def kalshi_market(future_time: datetime) -> Market:
    return Market(
        venue=Venue.KALSHI,
        venue_market_id="FED-JUNE-CUT",
        question="Will the Fed cut rates in June?",
        question_normalized="",
        close_time=future_time,
    )


@pytest.fixture()
def poly_book() -> OrderBook:
    return OrderBook(
        venue_market_id="cond_abc123",
        outcome="YES",
        asks=BookSide(levels=[BookLevel(price=0.55, size=100.0)]),
        bids=BookSide(levels=[BookLevel(price=0.53, size=80.0)]),
        snapshot_ts=datetime.now(timezone.utc),
    )


@pytest.fixture()
def kalshi_book() -> OrderBook:
    return OrderBook(
        venue_market_id="FED-JUNE-CUT",
        outcome="NO",
        asks=BookSide(levels=[BookLevel(price=0.45, size=90.0)]),
        bids=BookSide(levels=[BookLevel(price=0.43, size=70.0)]),
        snapshot_ts=datetime.now(timezone.utc),
    )


@pytest.fixture()
def matched_pair(poly_market: Market, kalshi_market: Market) -> EventPair:
    return EventPair(
        polymarket=poly_market,
        kalshi=kalshi_market,
        status=MatchStatus.MATCHED,
        confidence=MatchConfidence.EXACT,
    )


@pytest.fixture()
def tradeable_opportunity(
    matched_pair: EventPair,
    poly_book: OrderBook,
    kalshi_book: OrderBook,
) -> Opportunity:
    gross = 1.0 - (0.55 + 0.45)
    fee = 0.004
    slip = 0.010
    stale = 0.0
    net = round(gross - fee - slip - stale, 8)
    return Opportunity(
        pair=matched_pair,
        poly_book=poly_book,
        kalshi_book=kalshi_book,
        evaluated_at=datetime.now(timezone.utc),
        direction=TradeDirection.A,
        poly_ask=0.55,
        kalshi_ask=0.45,
        gross_edge=gross,
        fee_estimate=fee,
        slippage_buffer=slip,
        stale_penalty=stale,
        net_edge=net,
        executable_size_usdc=10.0,
        status=OpportunityStatus.TRADEABLE,
    )


@pytest.fixture()
def poly_leg() -> PositionLeg:
    return PositionLeg(
        venue="polymarket",
        venue_market_id="cond_abc123",
        side="YES",
        fill_price=0.55,
        contracts=18.18,
        notional_usdc=10.0,
        fee_paid_usdc=0.0,
    )


@pytest.fixture()
def kalshi_leg() -> PositionLeg:
    return PositionLeg(
        venue="kalshi",
        venue_market_id="FED-JUNE-CUT",
        side="NO",
        fill_price=0.45,
        contracts=22.22,
        notional_usdc=10.0,
        fee_paid_usdc=0.20,
    )


def make_position_id() -> str:
    return str(uuid4())
