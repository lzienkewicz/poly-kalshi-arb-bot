"""Unit tests for src/venues/polymarket/_parser.py — no HTTP, no async."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.market import Venue
from src.venues.polymarket._parser import (
    PolymarketBookRaw,
    PolymarketMarketRaw,
    extract_book,
    extract_market,
    to_market,
    to_orderbook,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MARKET_PAYLOAD: dict = {
    "condition_id": "0xabc123",
    "question": "Will the Fed cut rates by June 2025?",
    "active": True,
    "closed": False,
    "tokens": [
        {"token_id": "111", "outcome": "Yes"},
        {"token_id": "222", "outcome": "No"},
    ],
    "end_date_iso": "2025-06-30T18:00:00Z",
    "category": "Economy",
    "description": "Resolution based on FOMC decision.",
    "neg_risk": False,
    "minimum_tick_size": 0.01,
}

BOOK_YES_PAYLOAD: dict = {
    "market": "0xabc123",
    "asset_id": "111",
    "bids": [{"price": "0.54", "size": "100.0"}, {"price": "0.52", "size": "200.0"}],
    "asks": [{"price": "0.55", "size": "500.0"}, {"price": "0.57", "size": "1000.0"}],
}

BOOK_NO_PAYLOAD: dict = {
    "market": "0xabc123",
    "asset_id": "222",
    "bids": [{"price": "0.43", "size": "80.0"}],
    "asks": [{"price": "0.46", "size": "300.0"}, {"price": "0.48", "size": "800.0"}],
}

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# extract_market
# ---------------------------------------------------------------------------

def test_extract_market_all_fields():
    parsed = extract_market(MARKET_PAYLOAD)
    assert parsed.condition_id == "0xabc123"
    assert parsed.question == "Will the Fed cut rates by June 2025?"
    assert parsed.status == "active"
    assert parsed.end_date_iso == "2025-06-30T18:00:00Z"
    assert parsed.category == "Economy"
    assert parsed.yes_token_id == "111"
    assert parsed.no_token_id == "222"
    assert parsed.neg_risk is False
    assert parsed.raw is MARKET_PAYLOAD


def test_extract_market_status_active_when_active_true():
    raw = {**MARKET_PAYLOAD, "active": True, "closed": False}
    assert extract_market(raw).status == "active"


def test_extract_market_status_closed_when_closed_true():
    raw = {**MARKET_PAYLOAD, "closed": True, "active": False}
    assert extract_market(raw).status == "closed"


def test_extract_market_status_closed_takes_precedence():
    raw = {**MARKET_PAYLOAD, "closed": True, "active": True}
    assert extract_market(raw).status == "closed"


def test_extract_market_status_unknown_when_both_false():
    raw = {**MARKET_PAYLOAD, "closed": False, "active": False}
    assert extract_market(raw).status == "unknown"


def test_extract_market_tokens_extracted():
    parsed = extract_market(MARKET_PAYLOAD)
    assert parsed.yes_token_id == "111"
    assert parsed.no_token_id == "222"


def test_extract_market_tokens_case_insensitive():
    raw = {**MARKET_PAYLOAD, "tokens": [
        {"token_id": "aaa", "outcome": "YES"},
        {"token_id": "bbb", "outcome": "NO"},
    ]}
    parsed = extract_market(raw)
    assert parsed.yes_token_id == "aaa"
    assert parsed.no_token_id == "bbb"


def test_extract_market_missing_tokens():
    raw = {**MARKET_PAYLOAD, "tokens": []}
    parsed = extract_market(raw)
    assert parsed.yes_token_id is None
    assert parsed.no_token_id is None


def test_extract_market_preserves_raw():
    parsed = extract_market(MARKET_PAYLOAD)
    assert parsed.raw["condition_id"] == "0xabc123"


def test_extract_market_missing_optional_fields():
    raw = {"condition_id": "0xminimal", "active": True, "closed": False}
    parsed = extract_market(raw)
    assert parsed.question == ""
    assert parsed.end_date_iso is None
    assert parsed.category is None
    assert parsed.description is None
    assert parsed.yes_token_id is None
    assert parsed.no_token_id is None


def test_extract_market_neg_risk_true():
    raw = {**MARKET_PAYLOAD, "neg_risk": True}
    assert extract_market(raw).neg_risk is True


# ---------------------------------------------------------------------------
# extract_book
# ---------------------------------------------------------------------------

def test_extract_book_fields():
    parsed = extract_book(BOOK_YES_PAYLOAD, "0xabc123", "111", "YES")
    assert parsed.condition_id == "0xabc123"
    assert parsed.token_id == "111"
    assert parsed.outcome == "YES"
    assert parsed.asks == [(0.55, 500.0), (0.57, 1000.0)]
    assert parsed.bids == [(0.54, 100.0), (0.52, 200.0)]


def test_extract_book_outcome_uppercased():
    parsed = extract_book(BOOK_NO_PAYLOAD, "0xabc123", "222", "no")
    assert parsed.outcome == "NO"


def test_extract_book_empty_sides():
    raw = {"bids": [], "asks": []}
    parsed = extract_book(raw, "0x1", "tok1", "YES")
    assert parsed.bids == []
    assert parsed.asks == []


def test_extract_book_missing_sides():
    parsed = extract_book({}, "0x1", "tok1", "YES")
    assert parsed.bids == []
    assert parsed.asks == []


def test_extract_book_malformed_levels_skipped():
    raw = {
        "bids": [],
        "asks": [{"price": "0.55", "size": "500"}, {"bad": "data"}, {"price": "0.57", "size": "1000"}],
    }
    parsed = extract_book(raw, "0x1", "tok1", "YES")
    assert parsed.asks == [(0.55, 500.0), (0.57, 1000.0)]


def test_extract_book_prices_as_strings():
    raw = {"bids": [{"price": "0.54", "size": "100"}], "asks": [{"price": "0.55", "size": "200"}]}
    parsed = extract_book(raw, "0x1", "tok1", "YES")
    assert parsed.bids[0] == pytest.approx((0.54, 100.0))
    assert parsed.asks[0] == pytest.approx((0.55, 200.0))


# ---------------------------------------------------------------------------
# to_market
# ---------------------------------------------------------------------------

def test_to_market_venue_and_id():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.venue == Venue.POLYMARKET
    assert market.venue_market_id == "0xabc123"


def test_to_market_question_stored():
    market = to_market(extract_market(MARKET_PAYLOAD))
    assert market.question == "Will the Fed cut rates by June 2025?"


def test_to_market_question_normalized():
    market = to_market(extract_market(MARKET_PAYLOAD))
    assert market.question_normalized == "will the fed cut rates by june 2025"


def test_to_market_is_open_true():
    market = to_market(extract_market(MARKET_PAYLOAD))
    assert market.is_open is True


def test_to_market_is_open_false_when_closed():
    raw = {**MARKET_PAYLOAD, "closed": True, "active": False}
    market = to_market(extract_market(raw))
    assert market.is_open is False


def test_to_market_close_time_utc():
    market = to_market(extract_market(MARKET_PAYLOAD))
    assert market.close_time.tzinfo == timezone.utc
    assert market.close_time.year == 2025


def test_to_market_no_end_date_uses_sentinel():
    raw = {**MARKET_PAYLOAD, "end_date_iso": None}
    market = to_market(extract_market(raw))
    assert market.close_time.year == 9999


def test_to_market_category():
    market = to_market(extract_market(MARKET_PAYLOAD))
    assert market.category == "Economy"


def test_to_market_raw_preserved():
    market = to_market(extract_market(MARKET_PAYLOAD))
    assert market.raw["condition_id"] == "0xabc123"


# ---------------------------------------------------------------------------
# to_orderbook
# ---------------------------------------------------------------------------

def test_to_orderbook_outcome_yes():
    parsed = extract_book(BOOK_YES_PAYLOAD, "0xabc123", "111", "YES")
    book = to_orderbook(parsed, NOW)
    assert book.outcome == "YES"
    assert book.venue_market_id == "0xabc123"


def test_to_orderbook_best_ask():
    parsed = extract_book(BOOK_YES_PAYLOAD, "0xabc123", "111", "YES")
    book = to_orderbook(parsed, NOW)
    assert book.best_ask == pytest.approx(0.55)


def test_to_orderbook_best_bid():
    parsed = extract_book(BOOK_YES_PAYLOAD, "0xabc123", "111", "YES")
    book = to_orderbook(parsed, NOW)
    assert book.best_bid == pytest.approx(0.54)


def test_to_orderbook_asks_sorted_ascending():
    raw = {"asks": [{"price": "0.57", "size": "100"}, {"price": "0.55", "size": "500"}], "bids": []}
    parsed = extract_book(raw, "0x1", "tok", "YES")
    book = to_orderbook(parsed, NOW)
    prices = [lvl.price for lvl in book.asks.levels]
    assert prices == sorted(prices)


def test_to_orderbook_bids_sorted_descending():
    raw = {"bids": [{"price": "0.52", "size": "200"}, {"price": "0.54", "size": "100"}], "asks": []}
    parsed = extract_book(raw, "0x1", "tok", "YES")
    book = to_orderbook(parsed, NOW)
    prices = [lvl.price for lvl in book.bids.levels]
    assert prices == sorted(prices, reverse=True)


def test_to_orderbook_depth_sizes():
    parsed = extract_book(BOOK_YES_PAYLOAD, "0xabc123", "111", "YES")
    book = to_orderbook(parsed, NOW)
    assert book.asks.levels[0].size == 500.0
    assert book.asks.levels[1].size == 1000.0


def test_to_orderbook_available_at_ask():
    parsed = extract_book(BOOK_YES_PAYLOAD, "0xabc123", "111", "YES")
    book = to_orderbook(parsed, NOW)
    assert book.available_at_ask() == 500.0


def test_to_orderbook_snapshot_ts():
    parsed = extract_book(BOOK_YES_PAYLOAD, "0xabc123", "111", "YES")
    book = to_orderbook(parsed, NOW)
    assert book.snapshot_ts == NOW


def test_to_orderbook_out_of_range_price_skipped():
    raw = {"asks": [{"price": "1.05", "size": "100"}, {"price": "0.55", "size": "200"}], "bids": []}
    parsed = extract_book(raw, "0x1", "tok", "YES")
    book = to_orderbook(parsed, NOW)
    assert len(book.asks.levels) == 1
    assert book.best_ask == pytest.approx(0.55)


def test_to_orderbook_no_side():
    parsed = extract_book(BOOK_NO_PAYLOAD, "0xabc123", "222", "NO")
    book = to_orderbook(parsed, NOW)
    assert book.outcome == "NO"
    assert book.best_ask == pytest.approx(0.46)
    assert book.best_bid == pytest.approx(0.43)
