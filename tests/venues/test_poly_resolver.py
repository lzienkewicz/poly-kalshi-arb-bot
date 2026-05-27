"""
Tests for the Polymarket condition-ID resolver and Gamma-price fallback.

All HTTP calls are intercepted by respx — no real network traffic.

The tests cover three behaviours:
  1. Correct endpoint: condition_id resolves via GET /markets?condition_id=...
                       NOT via GET /markets/{condition_id} (that returns 422).
  2. Not-found:        empty list response → adapter returns None cleanly.
  3. 422 / error:      adapter swallows the error and returns None.
  4. Gamma fallback:   build_orderbooks_from_gamma_prices constructs synthetic
                       books from bestAsk/bestBid/lastTradePrice fields.
  5. Token extraction: _poly_token_ids handles both Gamma payload formats
                       (tokens array and clobTokenIds + outcomes string).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from src.models.market import Market, Venue
from src.models.orderbook import OrderBook
from src.venues.polymarket.adapter import PolymarketAdapter
from src.venues.polymarket.client import PolymarketClient

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

CONDITION_ID = "0xc510176cf39d73295354fc797d84b510d050b45d29d6c9735ee18cdbc4fd6463"

MARKET_PAYLOAD = {
    "condition_id": CONDITION_ID,
    "question": "Will JD Vance win the 2028 Republican presidential nomination?",
    "active": True,
    "closed": False,
    "tokens": [
        {"token_id": "tok_yes_111", "outcome": "Yes"},
        {"token_id": "tok_no_222",  "outcome": "No"},
    ],
    "end_date_iso": "2028-07-19T00:00:00Z",
    "category": "Politics",
    "neg_risk": False,
    "bestAsk": "0.47",
    "bestBid": "0.45",
    "lastTradePrice": "0.46",
}

MARKET_CLOB_FORMAT = {
    "conditionId": CONDITION_ID,
    "question": "Will JD Vance win the 2028 Republican presidential nomination?",
    "active": True,
    "closed": False,
    "clobTokenIds": ["tok_yes_333", "tok_no_444"],
    "outcomes": '["Yes","No"]',
    "endDate": "2028-07-19T00:00:00Z",
    "category": "Politics",
    "negRisk": False,
}

BOOK_YES = {
    "market": CONDITION_ID,
    "asset_id": "tok_yes_111",
    "bids": [{"price": "0.44", "size": "200.0"}],
    "asks": [{"price": "0.47", "size": "800.0"}],
}
BOOK_NO = {
    "market": CONDITION_ID,
    "asset_id": "tok_no_222",
    "bids": [{"price": "0.51", "size": "150.0"}],
    "asks": [{"price": "0.55", "size": "600.0"}],
}


def _make_adapter(http: httpx.AsyncClient) -> PolymarketAdapter:
    client = PolymarketClient(_http=http, max_attempts=2)
    return PolymarketAdapter(client)


# ---------------------------------------------------------------------------
# 1.  Correct endpoint: query parameter, not path
# ---------------------------------------------------------------------------

@respx.mock
async def test_condition_id_uses_query_param_not_path():
    """The resolver must call /markets?condition_id=... — never /markets/0x..."""
    query_route = respx.get(
        GAMMA_BASE + "/markets",
        params={"condition_id": CONDITION_ID},
    ).mock(return_value=httpx.Response(200, json=[MARKET_PAYLOAD]))

    # If the adapter accidentally calls the path endpoint it should NOT be hit
    path_route = respx.get(GAMMA_BASE + f"/markets/{CONDITION_ID}").mock(
        return_value=httpx.Response(422, json={"error": "id is invalid"})
    )

    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).resolve_market_by_condition_id(CONDITION_ID)

    assert market is not None
    assert query_route.called
    assert not path_route.called, "adapter must not call the path-based endpoint"


@respx.mock
async def test_resolved_market_fields():
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        return_value=httpx.Response(200, json=[MARKET_PAYLOAD])
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).resolve_market_by_condition_id(CONDITION_ID)

    assert market is not None
    assert market.venue == Venue.POLYMARKET
    assert market.venue_market_id == CONDITION_ID
    assert "jd vance" in market.question_normalized
    assert market.is_open is True


# ---------------------------------------------------------------------------
# 2.  Not-found: empty list → adapter returns None
# ---------------------------------------------------------------------------

@respx.mock
async def test_condition_id_not_found_returns_none():
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).resolve_market_by_condition_id(CONDITION_ID)

    assert market is None


# ---------------------------------------------------------------------------
# 3.  Error paths: 422 and network errors → adapter returns None
# ---------------------------------------------------------------------------

@respx.mock
async def test_condition_id_422_returns_none():
    """A 422 from the Gamma API must produce None, not an exception."""
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        return_value=httpx.Response(422, json={"error": "id is invalid"})
    )
    with patch("src.venues.polymarket.client.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).resolve_market_by_condition_id(CONDITION_ID)

    assert market is None


@respx.mock
async def test_condition_id_network_error_returns_none():
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    with patch("src.venues.polymarket.client.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).resolve_market_by_condition_id(CONDITION_ID)

    assert market is None


# ---------------------------------------------------------------------------
# 4.  Gamma-price fallback: build synthetic OrderBooks from market fields
# ---------------------------------------------------------------------------

def test_build_orderbooks_from_bestask_bestbid():
    yes_book, no_book = PolymarketAdapter.build_orderbooks_from_gamma_prices(
        CONDITION_ID,
        {"bestAsk": "0.47", "bestBid": "0.45"},
    )
    assert yes_book.outcome == "YES"
    assert no_book.outcome == "NO"
    assert yes_book.best_ask == pytest.approx(0.47)
    assert yes_book.best_bid == pytest.approx(0.45)
    # NO side is complementary: no_ask = 1 - yes_bid = 0.55, no_bid = 1 - yes_ask = 0.53
    assert no_book.best_ask == pytest.approx(0.55)
    assert no_book.best_bid == pytest.approx(0.53)


def test_build_orderbooks_depth_is_zero():
    yes_book, _ = PolymarketAdapter.build_orderbooks_from_gamma_prices(
        CONDITION_ID,
        {"bestAsk": "0.47", "bestBid": "0.45"},
    )
    # Synthetic books carry 0 size — depth checks in edge calc will reject them
    assert yes_book.asks.total_available == 0.0


def test_build_orderbooks_from_last_trade_price():
    """When only lastTradePrice is present, apply ±1¢ spread."""
    yes_book, no_book = PolymarketAdapter.build_orderbooks_from_gamma_prices(
        CONDITION_ID,
        {"lastTradePrice": "0.50"},
    )
    assert yes_book.best_ask == pytest.approx(0.51)
    assert yes_book.best_bid == pytest.approx(0.49)
    assert no_book.best_ask is not None


def test_build_orderbooks_no_prices_returns_empty_books():
    yes_book, no_book = PolymarketAdapter.build_orderbooks_from_gamma_prices(
        CONDITION_ID, {}
    )
    assert yes_book.best_ask is None
    assert no_book.best_ask is None


def test_build_orderbooks_snake_case_fields():
    """Adapter must also read best_ask / best_bid (snake_case Gamma variants)."""
    yes_book, _ = PolymarketAdapter.build_orderbooks_from_gamma_prices(
        CONDITION_ID,
        {"best_ask": "0.60", "best_bid": "0.58"},
    )
    assert yes_book.best_ask == pytest.approx(0.60)


def test_build_orderbooks_venue_market_id():
    yes_book, no_book = PolymarketAdapter.build_orderbooks_from_gamma_prices(
        CONDITION_ID, {"bestAsk": "0.47", "bestBid": "0.45"}
    )
    assert yes_book.venue_market_id == CONDITION_ID
    assert no_book.venue_market_id == CONDITION_ID


# ---------------------------------------------------------------------------
# 5.  Token-ID extraction — both Gamma payload formats
# ---------------------------------------------------------------------------

@respx.mock
async def test_token_ids_extracted_from_tokens_array():
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        return_value=httpx.Response(200, json=[MARKET_PAYLOAD])
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).resolve_market_by_condition_id(CONDITION_ID)

    assert market is not None
    # Use the same extractor that pricing_diagnostics uses
    from pricing_diagnostics import _poly_token_ids

    yes_id, no_id = _poly_token_ids(market)
    assert yes_id == "tok_yes_111"
    assert no_id == "tok_no_222"


@respx.mock
async def test_token_ids_extracted_from_clob_format():
    """clobTokenIds + outcomes JSON string (Format B) must also be handled."""
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        return_value=httpx.Response(200, json=[MARKET_CLOB_FORMAT])
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).resolve_market_by_condition_id(CONDITION_ID)

    assert market is not None
    from pricing_diagnostics import _poly_token_ids

    yes_id, no_id = _poly_token_ids(market)
    assert yes_id == "tok_yes_333"
    assert no_id == "tok_no_444"


# ---------------------------------------------------------------------------
# 6.  End-to-end: full fetch with CLOB succeeds (no fallback)
# ---------------------------------------------------------------------------

@respx.mock
async def test_full_clob_fetch_no_fallback():
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        return_value=httpx.Response(200, json=[MARKET_PAYLOAD])
    )
    respx.get(CLOB_BASE + "/book", params={"token_id": "tok_yes_111"}).mock(
        return_value=httpx.Response(200, json=BOOK_YES)
    )
    respx.get(CLOB_BASE + "/book", params={"token_id": "tok_no_222"}).mock(
        return_value=httpx.Response(200, json=BOOK_NO)
    )

    from pricing_diagnostics import _fetch_poly

    async with httpx.AsyncClient() as http:
        adapter = _make_adapter(http)
        market, yes_book, no_book, fallback_used = await _fetch_poly(adapter, CONDITION_ID)

    assert fallback_used is False
    assert market.venue_market_id == CONDITION_ID
    assert yes_book.best_ask == pytest.approx(0.47)
    assert no_book.best_ask == pytest.approx(0.55)
    # Real CLOB books have non-zero size
    assert yes_book.asks.total_available > 0


@respx.mock
async def test_clob_failure_triggers_gamma_fallback():
    """When CLOB orderbook fetch fails, synthetic books from Gamma prices are returned."""
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": CONDITION_ID}).mock(
        return_value=httpx.Response(200, json=[MARKET_PAYLOAD])
    )
    # CLOB returns 500 — adapter should fall back to Gamma prices
    respx.get(CLOB_BASE + "/book").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )

    from pricing_diagnostics import _fetch_poly

    with patch("src.venues.polymarket.client.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as http:
            market, yes_book, no_book, fallback_used = await _fetch_poly(
                _make_adapter(http), CONDITION_ID
            )

    assert fallback_used is True
    assert market.venue_market_id == CONDITION_ID
    # Gamma bestAsk = 0.47 → yes_ask = 0.47
    assert yes_book.best_ask == pytest.approx(0.47)
    # Synthetic books have zero depth
    assert yes_book.asks.total_available == 0.0


@respx.mock
async def test_unknown_condition_id_raises():
    """A condition_id not in Gamma returns RuntimeError, not a silent None."""
    respx.get(GAMMA_BASE + "/markets", params={"condition_id": "0xdeadbeef"}).mock(
        return_value=httpx.Response(200, json=[])
    )

    from pricing_diagnostics import _fetch_poly

    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="poly_condition_id_not_found"):
            await _fetch_poly(_make_adapter(http), "0xdeadbeef")
