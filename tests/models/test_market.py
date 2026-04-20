from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.models.market import Market, MarketStatus, Side, Venue


def test_market_creates(poly_market: Market):
    assert poly_market.id == "cond_abc123"
    assert poly_market.venue == Venue.POLYMARKET
    assert poly_market.status == MarketStatus.OPEN


def test_question_normalized_strips_punctuation():
    m = Market(
        id="x",
        venue=Venue.POLYMARKET,
        question="Will the Fed cut rates? Yes!",
        close_time=datetime(2099, 1, 1, tzinfo=timezone.utc),
        status=MarketStatus.OPEN,
    )
    assert m.question_normalized == "will the fed cut rates yes"


def test_question_normalized_lowercase():
    m = Market(
        id="x",
        venue=Venue.POLYMARKET,
        question="UPPER CASE Question",
        close_time=datetime(2099, 1, 1, tzinfo=timezone.utc),
        status=MarketStatus.OPEN,
    )
    assert m.question_normalized == "upper case question"


def test_is_open_true(poly_market: Market):
    assert poly_market.is_open is True


def test_is_open_false_when_closed(future_time: datetime):
    m = Market(
        id="x",
        venue=Venue.KALSHI,
        question="Q",
        close_time=future_time,
        status=MarketStatus.CLOSED,
    )
    assert m.is_open is False


def test_is_expired_false_for_future(poly_market: Market):
    assert poly_market.is_expired is False


def test_is_expired_true_for_past(past_time: datetime):
    m = Market(
        id="x",
        venue=Venue.POLYMARKET,
        question="Old question",
        close_time=past_time,
        status=MarketStatus.RESOLVED,
    )
    assert m.is_expired is True


def test_side_opposite():
    assert Side.YES.opposite() == Side.NO
    assert Side.NO.opposite() == Side.YES


def test_market_is_frozen(poly_market: Market):
    with pytest.raises(Exception):
        poly_market.id = "new_id"  # type: ignore[misc]


def test_resolver_defaults_to_none(poly_market: Market):
    assert poly_market.resolver is None


def test_resolver_stored():
    m = Market(
        id="x",
        venue=Venue.KALSHI,
        question="Q",
        close_time=datetime(2099, 1, 1, tzinfo=timezone.utc),
        status=MarketStatus.OPEN,
        resolver="AP",
    )
    assert m.resolver == "AP"


def test_invalid_status_raises():
    with pytest.raises(ValidationError):
        Market(
            id="x",
            venue=Venue.POLYMARKET,
            question="Q",
            close_time=datetime(2099, 1, 1, tzinfo=timezone.utc),
            status="invalid_status",  # type: ignore[arg-type]
        )
