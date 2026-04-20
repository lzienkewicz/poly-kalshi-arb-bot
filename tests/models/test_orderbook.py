from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.models.orderbook import BookLevel, BookSide, OrderBook


def test_book_level_creates():
    bl = BookLevel(price=0.55, size=100.0)
    assert bl.price == 0.55
    assert bl.size == 100.0


def test_book_level_price_bounds():
    with pytest.raises(ValidationError):
        BookLevel(price=-0.01, size=10.0)
    with pytest.raises(ValidationError):
        BookLevel(price=1.01, size=10.0)


def test_book_level_size_non_negative():
    with pytest.raises(ValidationError):
        BookLevel(price=0.5, size=-1.0)


def test_book_side_best_price():
    side = BookSide(levels=[BookLevel(price=0.55, size=100.0), BookLevel(price=0.57, size=50.0)])
    assert side.best_price == 0.55


def test_book_side_empty():
    side = BookSide()
    assert side.best_price is None
    assert side.best_size is None
    assert side.total_available == 0.0


def test_book_side_available_at_price():
    side = BookSide(levels=[BookLevel(price=0.55, size=100.0), BookLevel(price=0.55, size=20.0)])
    assert side.available_at_price(0.55) == 120.0
    assert side.available_at_price(0.60) == 0.0


def test_orderbook_creates(poly_book: OrderBook):
    assert poly_book.venue_market_id == "cond_abc123"
    assert poly_book.outcome == "YES"
    assert poly_book.best_ask == 0.55
    assert poly_book.best_bid == 0.53


def test_orderbook_mid():
    book = OrderBook(
        venue_market_id="x",
        outcome="YES",
        asks=BookSide(levels=[BookLevel(price=0.56, size=10.0)]),
        bids=BookSide(levels=[BookLevel(price=0.54, size=10.0)]),
        snapshot_ts=datetime.now(timezone.utc),
    )
    assert book.mid == pytest.approx(0.55)


def test_orderbook_spread():
    book = OrderBook(
        venue_market_id="x",
        outcome="YES",
        asks=BookSide(levels=[BookLevel(price=0.56, size=10.0)]),
        bids=BookSide(levels=[BookLevel(price=0.54, size=10.0)]),
        snapshot_ts=datetime.now(timezone.utc),
    )
    assert book.spread == pytest.approx(0.02)


def test_orderbook_empty_bids_mid_is_none():
    book = OrderBook(
        venue_market_id="x",
        outcome="YES",
        asks=BookSide(levels=[BookLevel(price=0.56, size=10.0)]),
        snapshot_ts=datetime.now(timezone.utc),
    )
    assert book.mid is None
    assert book.spread is None


def test_crossed_book_raises():
    with pytest.raises(ValidationError, match="crossed book"):
        OrderBook(
            venue_market_id="x",
            outcome="YES",
            asks=BookSide(levels=[BookLevel(price=0.50, size=10.0)]),
            bids=BookSide(levels=[BookLevel(price=0.55, size=10.0)]),
            snapshot_ts=datetime.now(timezone.utc),
        )


def test_is_stale_fresh(poly_book: OrderBook):
    assert poly_book.is_stale(max_age_s=30) is False


def test_is_stale_old():
    book = OrderBook(
        venue_market_id="x",
        outcome="YES",
        snapshot_ts=datetime.now(timezone.utc) - timedelta(seconds=60),
    )
    assert book.is_stale(max_age_s=30) is True


def test_is_stale_with_explicit_now(poly_book: OrderBook):
    future_now = poly_book.snapshot_ts + timedelta(seconds=60)
    assert poly_book.is_stale(max_age_s=30, now=future_now) is True
    past_now = poly_book.snapshot_ts + timedelta(seconds=10)
    assert poly_book.is_stale(max_age_s=30, now=past_now) is False


def test_available_at_ask(poly_book: OrderBook):
    assert poly_book.available_at_ask() == 100.0


def test_available_at_ask_empty_book():
    book = OrderBook(
        venue_market_id="x",
        outcome="NO",
        snapshot_ts=datetime.now(timezone.utc),
    )
    assert book.available_at_ask() == 0.0


def test_orderbook_is_frozen(poly_book: OrderBook):
    with pytest.raises(Exception):
        poly_book.outcome = "NO"  # type: ignore[misc]
