from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.market import Market, MarketStatus, Venue
from src.models.orderbook import OrderBook, PriceLevel


@pytest.fixture()
def future_time() -> datetime:
    return datetime(2099, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def past_time() -> datetime:
    return datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def poly_market(future_time: datetime) -> Market:
    return Market(
        id="cond_abc123",
        venue=Venue.POLYMARKET,
        question="Will the Fed cut rates in June?",
        close_time=future_time,
        status=MarketStatus.OPEN,
    )


@pytest.fixture()
def kalshi_market(future_time: datetime) -> Market:
    return Market(
        id="FED-JUNE-CUT",
        venue=Venue.KALSHI,
        question="Will the Fed cut rates in June?",
        close_time=future_time,
        status=MarketStatus.OPEN,
    )


@pytest.fixture()
def poly_book(poly_market: Market) -> OrderBook:
    return OrderBook(
        venue=Venue.POLYMARKET,
        market_id=poly_market.id,
        yes_ask=PriceLevel(price=0.55, size=100.0),
        no_ask=PriceLevel(price=0.46, size=80.0),
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.fixture()
def kalshi_book(kalshi_market: Market) -> OrderBook:
    return OrderBook(
        venue=Venue.KALSHI,
        market_id=kalshi_market.id,
        yes_ask=PriceLevel(price=0.56, size=90.0),
        no_ask=PriceLevel(price=0.45, size=70.0),
        fetched_at=datetime.now(timezone.utc),
    )
