from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.models.market import Market, Venue, _normalize_question


def test_market_creates(poly_market: Market):
    assert poly_market.venue_market_id == "cond_abc123"
    assert poly_market.venue == Venue.POLYMARKET
    assert poly_market.is_open is True


def test_question_normalized_auto_derived(poly_market: Market):
    assert poly_market.question_normalized == "will the fed cut rates in june"


def test_question_normalized_explicit():
    m = Market(
        venue=Venue.POLYMARKET,
        venue_market_id="x",
        question="Will X happen?",
        question_normalized="custom normalized",
        close_time=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    assert m.question_normalized == "custom normalized"


def test_normalize_question_strips_punctuation():
    assert _normalize_question("Will the Fed cut rates? Yes!") == "will the fed cut rates yes"


def test_normalize_question_lowercase():
    assert _normalize_question("UPPER CASE") == "upper case"


def test_normalize_question_strips_special_chars():
    assert _normalize_question("50% chance — really?") == "50 chance  really"


def test_is_open_default_true(poly_market: Market):
    assert poly_market.is_open is True


def test_is_open_can_be_false(future_time: datetime):
    m = Market(
        venue=Venue.KALSHI,
        venue_market_id="x",
        question="Q",
        question_normalized="q",
        close_time=future_time,
        is_open=False,
    )
    assert m.is_open is False


def test_resolution_source_default_none(poly_market: Market):
    assert poly_market.resolution_source is None


def test_resolution_source_stored(future_time: datetime):
    m = Market(
        venue=Venue.KALSHI,
        venue_market_id="x",
        question="Q",
        question_normalized="q",
        close_time=future_time,
        resolution_source="AP",
    )
    assert m.resolution_source == "AP"


def test_raw_defaults_empty(poly_market: Market):
    assert poly_market.raw == {}


def test_market_is_frozen(poly_market: Market):
    with pytest.raises(Exception):
        poly_market.venue_market_id = "other"  # type: ignore[misc]


def test_invalid_venue_raises():
    with pytest.raises(ValidationError):
        Market(
            venue="badvenue",  # type: ignore[arg-type]
            venue_market_id="x",
            question="Q",
            question_normalized="q",
            close_time=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
