from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.models.market import Venue
from src.models.orderbook import OrderBook, PriceLevel


def test_price_level_creates():
    pl = PriceLevel(price=0.55, size=100.0)
    assert pl.price == 0.55
    assert pl.size == 100.0


def test_price_level_bounds():
    with pytest.raises(ValidationError):
        PriceLevel(price=-0.01, size=10.0)
    with pytest.raises(ValidationError):
        PriceLevel(price=1.01, size=10.0)
    with pytest.raises(ValidationError):
        PriceLevel(price=0.5, size=-1.0)


def test_price_level_boundary_values():
    PriceLevel(price=0.0, size=0.0)
    PriceLevel(price=1.0, size=999.0)


def test_orderbook_creates(poly_book: OrderBook):
    assert poly_book.venue == Venue.POLYMARKET
    assert poly_book.yes_ask.price == 0.55
    assert poly_book.no_ask.price == 0.46


def test_orderbook_optional_bids_default_none(poly_book: OrderBook):
    assert poly_book.yes_bid is None
    assert poly_book.no_bid is None


def test_orderbook_with_bids():
    book = OrderBook(
        venue=Venue.KALSHI,
        market_id="mkt1",
        yes_ask=PriceLevel(price=0.56, size=50.0),
        no_ask=PriceLevel(price=0.45, size=50.0),
        yes_bid=PriceLevel(price=0.54, size=40.0),
        no_bid=PriceLevel(price=0.43, size=40.0),
        fetched_at=datetime.now(timezone.utc),
    )
    assert book.yes_bid is not None
    assert book.yes_bid.price == 0.54


def test_is_stale_fresh(poly_book: OrderBook):
    assert poly_book.is_stale(max_age_s=30) is False


def test_is_stale_old():
    old_book = OrderBook(
        venue=Venue.POLYMARKET,
        market_id="mkt1",
        yes_ask=PriceLevel(price=0.55, size=100.0),
        no_ask=PriceLevel(price=0.46, size=80.0),
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=60),
    )
    assert old_book.is_stale(max_age_s=30) is True


def test_is_stale_clearly_within_limit():
    book = OrderBook(
        venue=Venue.POLYMARKET,
        market_id="mkt1",
        yes_ask=PriceLevel(price=0.55, size=100.0),
        no_ask=PriceLevel(price=0.46, size=80.0),
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    assert book.is_stale(max_age_s=30) is False


def test_orderbook_is_frozen(poly_book: OrderBook):
    with pytest.raises(Exception):
        poly_book.market_id = "other"  # type: ignore[misc]
