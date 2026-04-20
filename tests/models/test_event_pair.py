from __future__ import annotations

import pytest

from src.models.event_pair import EventPair, MatchConfidence
from src.models.market import Market


def test_event_pair_creates(poly_market: Market, kalshi_market: Market):
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.EXACT,
    )
    assert pair.confidence == MatchConfidence.EXACT
    assert pair.direction_inverted is False


def test_pair_id_format(poly_market: Market, kalshi_market: Market):
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.EXACT,
    )
    assert pair.pair_id == f"{poly_market.id}:{kalshi_market.id}"


def test_direction_inverted_default_false(poly_market: Market, kalshi_market: Market):
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.EXACT,
    )
    assert pair.direction_inverted is False


def test_direction_inverted_can_be_set(poly_market: Market, kalshi_market: Market):
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.PROBABLE,
        direction_inverted=True,
    )
    assert pair.direction_inverted is True


def test_all_confidence_levels(poly_market: Market, kalshi_market: Market):
    for level in MatchConfidence:
        pair = EventPair(
            poly_market=poly_market,
            kalshi_market=kalshi_market,
            confidence=level,
        )
        assert pair.confidence == level


def test_event_pair_is_frozen(poly_market: Market, kalshi_market: Market):
    pair = EventPair(
        poly_market=poly_market,
        kalshi_market=kalshi_market,
        confidence=MatchConfidence.EXACT,
    )
    with pytest.raises(Exception):
        pair.confidence = MatchConfidence.UNKNOWN  # type: ignore[misc]
