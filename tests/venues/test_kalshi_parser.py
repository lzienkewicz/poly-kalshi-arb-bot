"""Unit tests for src/venues/kalshi/_parser.py — no HTTP, no async."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.market import Venue
from src.venues.kalshi._parser import (
    KalshiMarketRaw,
    KalshiOrderbookRaw,
    cents_to_dollars,
    extract_market,
    extract_orderbook,
    to_market,
    to_orderbooks,
    to_orderbooks_from_market,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MARKET_PAYLOAD: dict = {
    "ticker": "KXFED-25JUN-T5.25",
    "title": "Will the Fed cut rates by June 2025?",
    "status": "open",
    "close_time": "2025-06-30T18:00:00Z",
    "expiration_time": "2025-06-30T18:00:00Z",
    "market_type": "binary",
    "yes_ask": 55,
    "no_ask": 46,
    "yes_bid": 53,
    "no_bid": 44,
    "category": "Economy",
    "settlement_sources": [{"name": "Federal Reserve", "url": "https://federalreserve.gov"}],
}

ORDERBOOK_PAYLOAD: dict = {
    "orderbook": {
        "yes": [[55, 500], [57, 1000]],
        "no": [[46, 300], [48, 800]],
    }
}

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# cents_to_dollars
# ---------------------------------------------------------------------------

def test_cents_to_dollars_typical():
    assert cents_to_dollars(55) == pytest.approx(0.55)


def test_cents_to_dollars_zero():
    assert cents_to_dollars(0) == 0.0


def test_cents_to_dollars_hundred():
    assert cents_to_dollars(100) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# extract_market
# ---------------------------------------------------------------------------

def test_extract_market_all_fields():
    parsed = extract_market(MARKET_PAYLOAD)
    assert parsed.ticker == "KXFED-25JUN-T5.25"
    assert parsed.title == "Will the Fed cut rates by June 2025?"
    assert parsed.status == "open"
    assert parsed.market_type == "binary"
    assert parsed.yes_ask_cents == 55
    assert parsed.no_ask_cents == 46
    assert parsed.yes_bid_cents == 53
    assert parsed.no_bid_cents == 44
    assert parsed.category == "Economy"
    assert parsed.resolution_source == "Federal Reserve"
    assert parsed.close_time_str == "2025-06-30T18:00:00Z"
    assert parsed.raw is MARKET_PAYLOAD


def test_extract_market_status_lowercased():
    raw = {**MARKET_PAYLOAD, "status": "OPEN"}
    parsed = extract_market(raw)
    assert parsed.status == "open"


def test_extract_market_missing_optional_fields():
    raw = {
        "ticker": "X",
        "title": "Q",
        "status": "open",
        "close_time": "2025-01-01T00:00:00Z",
    }
    parsed = extract_market(raw)
    assert parsed.yes_ask_cents is None
    assert parsed.no_ask_cents is None
    assert parsed.yes_bid_cents is None
    assert parsed.no_bid_cents is None
    assert parsed.category is None
    assert parsed.resolution_source is None
    assert parsed.expiration_time_str is None
    assert parsed.market_type == ""


def test_extract_market_closed_status():
    raw = {**MARKET_PAYLOAD, "status": "closed"}
    parsed = extract_market(raw)
    assert parsed.status == "closed"


def test_extract_market_settlement_source_fallback():
    raw = {**MARKET_PAYLOAD, "settlement_sources": [], "rules_primary": "Reuters"}
    parsed = extract_market(raw)
    assert parsed.resolution_source == "Reuters"


def test_extract_market_settlement_source_url_fallback():
    raw = {**MARKET_PAYLOAD, "settlement_sources": [{"url": "https://example.com"}]}
    parsed = extract_market(raw)
    assert parsed.resolution_source == "https://example.com"


def test_extract_market_preserves_raw():
    parsed = extract_market(MARKET_PAYLOAD)
    assert parsed.raw["ticker"] == "KXFED-25JUN-T5.25"


def test_extract_market_title_fallback_to_subtitle():
    raw = {**MARKET_PAYLOAD, "title": "", "subtitle": "fallback title"}
    parsed = extract_market(raw)
    assert parsed.title == "fallback title"


# ---------------------------------------------------------------------------
# extract_orderbook
# ---------------------------------------------------------------------------

def test_extract_orderbook_fields():
    parsed = extract_orderbook(ORDERBOOK_PAYLOAD, "KXFED-25JUN-T5.25")
    assert parsed.ticker == "KXFED-25JUN-T5.25"
    assert parsed.yes_levels == [(55, 500), (57, 1000)]
    assert parsed.no_levels == [(46, 300), (48, 800)]


def test_extract_orderbook_empty_sides():
    parsed = extract_orderbook({"orderbook": {"yes": [], "no": []}}, "X")
    assert parsed.yes_levels == []
    assert parsed.no_levels == []


def test_extract_orderbook_missing_sides():
    parsed = extract_orderbook({"orderbook": {}}, "X")
    assert parsed.yes_levels == []
    assert parsed.no_levels == []


def test_extract_orderbook_malformed_levels_skipped():
    raw = {"orderbook": {"yes": [[55, 500], ["bad", "data"], [57, 1000]], "no": []}}
    parsed = extract_orderbook(raw, "X")
    assert parsed.yes_levels == [(55, 500), (57, 1000)]


def test_extract_orderbook_accepts_nested_orderbook():
    """Handles both {"orderbook": {...}} and raw dict directly."""
    raw = {"yes": [[55, 500]], "no": [[46, 300]]}
    parsed = extract_orderbook(raw, "X")
    assert parsed.yes_levels == [(55, 500)]


# ---------------------------------------------------------------------------
# to_market
# ---------------------------------------------------------------------------

def test_to_market_venue_and_id():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.venue == Venue.KALSHI
    assert market.venue_market_id == "KXFED-25JUN-T5.25"


def test_to_market_question_stored():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.question == "Will the Fed cut rates by June 2025?"


def test_to_market_question_normalized():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.question_normalized == "will the fed cut rates by june 2025"


def test_to_market_is_open_true():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.is_open is True


def test_to_market_is_open_false_when_closed():
    raw = {**MARKET_PAYLOAD, "status": "closed"}
    market = to_market(extract_market(raw))
    assert market.is_open is False


def test_to_market_close_time_utc():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.close_time.tzinfo == timezone.utc
    assert market.close_time.year == 2025


def test_to_market_resolution_source():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.resolution_source == "Federal Reserve"


def test_to_market_category():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.category == "Economy"


def test_to_market_raw_preserved():
    parsed = extract_market(MARKET_PAYLOAD)
    market = to_market(parsed)
    assert market.raw["ticker"] == "KXFED-25JUN-T5.25"


# ---------------------------------------------------------------------------
# to_orderbooks
# ---------------------------------------------------------------------------

def test_to_orderbooks_yes_asks():
    parsed = extract_orderbook(ORDERBOOK_PAYLOAD, "KXFED-25JUN-T5.25")
    yes_book, _ = to_orderbooks(parsed, NOW)
    assert yes_book.outcome == "YES"
    assert yes_book.best_ask == pytest.approx(0.55)


def test_to_orderbooks_no_asks():
    parsed = extract_orderbook(ORDERBOOK_PAYLOAD, "KXFED-25JUN-T5.25")
    _, no_book = to_orderbooks(parsed, NOW)
    assert no_book.outcome == "NO"
    assert no_book.best_ask == pytest.approx(0.46)


def test_to_orderbooks_sorted_ascending():
    raw = {"orderbook": {"yes": [[57, 100], [55, 500]], "no": [[48, 100], [46, 300]]}}
    parsed = extract_orderbook(raw, "X")
    yes_book, _ = to_orderbooks(parsed, NOW)
    prices = [lvl.price for lvl in yes_book.asks.levels]
    assert prices == sorted(prices)


def test_to_orderbooks_depth_sizes():
    parsed = extract_orderbook(ORDERBOOK_PAYLOAD, "X")
    yes_book, _ = to_orderbooks(parsed, NOW)
    # First level: 500 contracts, second: 1000 contracts
    assert yes_book.asks.levels[0].size == 500.0
    assert yes_book.asks.levels[1].size == 1000.0


def test_to_orderbooks_out_of_range_price_skipped():
    raw = {"orderbook": {"yes": [[101, 100], [55, 200]], "no": []}}
    parsed = extract_orderbook(raw, "X")
    yes_book, _ = to_orderbooks(parsed, NOW)
    assert len(yes_book.asks.levels) == 1
    assert yes_book.best_ask == pytest.approx(0.55)


def test_to_orderbooks_available_at_ask():
    parsed = extract_orderbook(ORDERBOOK_PAYLOAD, "X")
    yes_book, _ = to_orderbooks(parsed, NOW)
    assert yes_book.available_at_ask() == 500.0


def test_to_orderbooks_snapshot_ts():
    parsed = extract_orderbook(ORDERBOOK_PAYLOAD, "X")
    yes_book, no_book = to_orderbooks(parsed, NOW)
    assert yes_book.snapshot_ts == NOW
    assert no_book.snapshot_ts == NOW


# ---------------------------------------------------------------------------
# to_orderbooks_from_market
# ---------------------------------------------------------------------------

def test_to_orderbooks_from_market_yes_ask():
    parsed = extract_market(MARKET_PAYLOAD)
    yes_book, _ = to_orderbooks_from_market(parsed, NOW)
    assert yes_book.best_ask == pytest.approx(0.55)


def test_to_orderbooks_from_market_no_ask():
    parsed = extract_market(MARKET_PAYLOAD)
    _, no_book = to_orderbooks_from_market(parsed, NOW)
    assert no_book.best_ask == pytest.approx(0.46)


def test_to_orderbooks_from_market_yes_bid():
    parsed = extract_market(MARKET_PAYLOAD)
    yes_book, _ = to_orderbooks_from_market(parsed, NOW)
    assert yes_book.best_bid == pytest.approx(0.53)


def test_to_orderbooks_from_market_no_bid():
    parsed = extract_market(MARKET_PAYLOAD)
    _, no_book = to_orderbooks_from_market(parsed, NOW)
    assert no_book.best_bid == pytest.approx(0.44)


def test_to_orderbooks_from_market_size_is_zero():
    parsed = extract_market(MARKET_PAYLOAD)
    yes_book, no_book = to_orderbooks_from_market(parsed, NOW)
    assert yes_book.asks.levels[0].size == 0.0
    assert no_book.asks.levels[0].size == 0.0


def test_to_orderbooks_from_market_missing_prices():
    raw = {
        "ticker": "X",
        "title": "Q",
        "status": "open",
        "close_time": "2025-01-01T00:00:00Z",
    }
    parsed = extract_market(raw)
    yes_book, no_book = to_orderbooks_from_market(parsed, NOW)
    assert yes_book.best_ask is None
    assert no_book.best_ask is None
