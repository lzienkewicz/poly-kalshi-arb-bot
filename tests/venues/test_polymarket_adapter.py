"""
Integration tests for PolymarketClient + PolymarketAdapter using respx HTTP mocks.

All HTTP calls are intercepted — no real network traffic.
asyncio.sleep is patched to a no-op in retry tests so the suite stays fast.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from src.models.market import Venue
from src.venues.polymarket.adapter import PolymarketAdapter
from src.venues.polymarket.client import PolymarketClient, PolymarketHTTPError

BASE = "https://clob.polymarket.com"

# ---------------------------------------------------------------------------
# Shared payloads
# ---------------------------------------------------------------------------

MARKET_1 = {
    "condition_id": "0xabc111",
    "question": "Will the Fed cut rates by June 2025?",
    "active": True,
    "closed": False,
    "tokens": [
        {"token_id": "t111yes", "outcome": "Yes"},
        {"token_id": "t111no", "outcome": "No"},
    ],
    "end_date_iso": "2025-06-30T18:00:00Z",
    "category": "Economy",
    "neg_risk": False,
}

MARKET_2 = {
    "condition_id": "0xabc222",
    "question": "Will candidate X win the 2025 election?",
    "active": True,
    "closed": False,
    "tokens": [
        {"token_id": "t222yes", "outcome": "Yes"},
        {"token_id": "t222no", "outcome": "No"},
    ],
    "end_date_iso": "2025-11-05T00:00:00Z",
    "category": "Politics",
    "neg_risk": False,
}

BOOK_YES = {
    "market": "0xabc111",
    "asset_id": "t111yes",
    "bids": [{"price": "0.54", "size": "100.0"}],
    "asks": [{"price": "0.55", "size": "500.0"}, {"price": "0.57", "size": "1000.0"}],
}

BOOK_NO = {
    "market": "0xabc111",
    "asset_id": "t111no",
    "bids": [{"price": "0.43", "size": "80.0"}],
    "asks": [{"price": "0.46", "size": "300.0"}],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(http: httpx.AsyncClient) -> PolymarketAdapter:
    client = PolymarketClient(_http=http, max_attempts=3)
    return PolymarketAdapter(client)


# ---------------------------------------------------------------------------
# fetch_open_markets — single page
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_open_markets_returns_markets():
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [MARKET_1, MARKET_2], "next_cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 2
    assert markets[0].venue_market_id == "0xabc111"
    assert markets[1].venue_market_id == "0xabc222"


@respx.mock
async def test_fetch_open_markets_venue_is_polymarket():
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [MARKET_1], "next_cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert markets[0].venue == Venue.POLYMARKET


@respx.mock
async def test_fetch_open_markets_filters_closed():
    closed = {**MARKET_1, "closed": True, "active": False}
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [closed, MARKET_2], "next_cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1
    assert markets[0].venue_market_id == "0xabc222"


@respx.mock
async def test_fetch_open_markets_filters_inactive():
    inactive = {**MARKET_1, "active": False, "closed": False}
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [inactive, MARKET_2], "next_cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1
    assert markets[0].venue_market_id == "0xabc222"


# ---------------------------------------------------------------------------
# fetch_open_markets — pagination
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_open_markets_follows_cursor():
    route = respx.get(f"{BASE}/markets")
    route.side_effect = [
        httpx.Response(200, json={"data": [MARKET_1], "next_cursor": "page2tok"}),
        httpx.Response(200, json={"data": [MARKET_2], "next_cursor": ""}),
    ]
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 2
    assert route.call_count == 2


@respx.mock
async def test_fetch_open_markets_stops_on_lte_cursor():
    route = respx.get(f"{BASE}/markets")
    route.side_effect = [
        httpx.Response(200, json={"data": [MARKET_1], "next_cursor": "LTE="}),
    ]
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1
    assert route.call_count == 1


@respx.mock
async def test_fetch_open_markets_stops_on_empty_cursor():
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [MARKET_1], "next_cursor": None})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1


# ---------------------------------------------------------------------------
# fetch_open_markets — malformed records
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_open_markets_skips_malformed_records():
    malformed = {"active": True, "closed": False}  # missing required condition_id
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [malformed, MARKET_1], "next_cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1
    assert markets[0].venue_market_id == "0xabc111"


# ---------------------------------------------------------------------------
# fetch_market — single market
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_market_returns_market():
    respx.get(f"{BASE}/markets/0xabc111").mock(
        return_value=httpx.Response(200, json=MARKET_1)
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).fetch_market("0xabc111")

    assert market.venue_market_id == "0xabc111"
    assert market.is_open is True


@respx.mock
async def test_fetch_market_question_normalized():
    respx.get(f"{BASE}/markets/0xabc111").mock(
        return_value=httpx.Response(200, json=MARKET_1)
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).fetch_market("0xabc111")

    assert market.question_normalized == "will the fed cut rates by june 2025"


# ---------------------------------------------------------------------------
# fetch_orderbooks
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_orderbooks_returns_yes_no():
    respx.get(f"{BASE}/book", params={"token_id": "t111yes"}).mock(
        return_value=httpx.Response(200, json=BOOK_YES)
    )
    respx.get(f"{BASE}/book", params={"token_id": "t111no"}).mock(
        return_value=httpx.Response(200, json=BOOK_NO)
    )
    async with httpx.AsyncClient() as http:
        yes_book, no_book = await _make_adapter(http).fetch_orderbooks(
            "0xabc111", "t111yes", "t111no"
        )

    assert yes_book.outcome == "YES"
    assert no_book.outcome == "NO"
    assert yes_book.best_ask == pytest.approx(0.55)
    assert no_book.best_ask == pytest.approx(0.46)


@respx.mock
async def test_fetch_orderbooks_depth_available():
    respx.get(f"{BASE}/book", params={"token_id": "t111yes"}).mock(
        return_value=httpx.Response(200, json=BOOK_YES)
    )
    respx.get(f"{BASE}/book", params={"token_id": "t111no"}).mock(
        return_value=httpx.Response(200, json=BOOK_NO)
    )
    async with httpx.AsyncClient() as http:
        yes_book, _ = await _make_adapter(http).fetch_orderbooks(
            "0xabc111", "t111yes", "t111no"
        )

    assert yes_book.available_at_ask() == 500.0


@respx.mock
async def test_fetch_orderbooks_token_id_sent_as_param():
    route_yes = respx.get(f"{BASE}/book", params={"token_id": "t111yes"}).mock(
        return_value=httpx.Response(200, json=BOOK_YES)
    )
    respx.get(f"{BASE}/book", params={"token_id": "t111no"}).mock(
        return_value=httpx.Response(200, json=BOOK_NO)
    )
    async with httpx.AsyncClient() as http:
        await _make_adapter(http).fetch_orderbooks("0xabc111", "t111yes", "t111no")

    assert "token_id" in str(route_yes.calls[0].request.url)


@respx.mock
async def test_fetch_orderbooks_bid_prices_present():
    respx.get(f"{BASE}/book", params={"token_id": "t111yes"}).mock(
        return_value=httpx.Response(200, json=BOOK_YES)
    )
    respx.get(f"{BASE}/book", params={"token_id": "t111no"}).mock(
        return_value=httpx.Response(200, json=BOOK_NO)
    )
    async with httpx.AsyncClient() as http:
        yes_book, no_book = await _make_adapter(http).fetch_orderbooks(
            "0xabc111", "t111yes", "t111no"
        )

    assert yes_book.best_bid == pytest.approx(0.54)
    assert no_book.best_bid == pytest.approx(0.43)


# ---------------------------------------------------------------------------
# fetch_top_of_book (delegates to fetch_orderbooks)
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_top_of_book_prices():
    respx.get(f"{BASE}/book", params={"token_id": "t111yes"}).mock(
        return_value=httpx.Response(200, json=BOOK_YES)
    )
    respx.get(f"{BASE}/book", params={"token_id": "t111no"}).mock(
        return_value=httpx.Response(200, json=BOOK_NO)
    )
    async with httpx.AsyncClient() as http:
        yes_book, no_book = await _make_adapter(http).fetch_top_of_book(
            "0xabc111", "t111yes", "t111no"
        )

    assert yes_book.best_ask == pytest.approx(0.55)
    assert no_book.best_ask == pytest.approx(0.46)


# ---------------------------------------------------------------------------
# Retry — 5xx
# ---------------------------------------------------------------------------

@respx.mock
async def test_retry_succeeds_after_500():
    route = respx.get(f"{BASE}/markets/0xabc111")
    route.side_effect = [
        httpx.Response(500, json={"error": "internal"}),
        httpx.Response(200, json=MARKET_1),
    ]
    with patch("src.venues.polymarket.client.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).fetch_market("0xabc111")

    assert market.venue_market_id == "0xabc111"
    assert route.call_count == 2


@respx.mock
async def test_raises_after_max_retries_on_500():
    respx.get(f"{BASE}/markets/0xabc111").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )
    with patch("src.venues.polymarket.client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(PolymarketHTTPError) as exc_info:
            async with httpx.AsyncClient() as http:
                await _make_adapter(http).fetch_market("0xabc111")

    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Retry — 429 rate limit
# ---------------------------------------------------------------------------

@respx.mock
async def test_retry_on_429_respects_retry_after():
    route = respx.get(f"{BASE}/markets/0xabc111")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "0.1"}, json={}),
        httpx.Response(200, json=MARKET_1),
    ]
    sleep_mock = AsyncMock()
    with patch("src.venues.polymarket.client.asyncio.sleep", sleep_mock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).fetch_market("0xabc111")

    assert market.venue_market_id == "0xabc111"
    sleep_mock.assert_called_once_with(pytest.approx(0.1, abs=0.01))


# ---------------------------------------------------------------------------
# No retry on 4xx client errors
# ---------------------------------------------------------------------------

@respx.mock
async def test_no_retry_on_404():
    route = respx.get(f"{BASE}/markets/UNKNOWN").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    with pytest.raises(PolymarketHTTPError) as exc_info:
        async with httpx.AsyncClient() as http:
            await _make_adapter(http).fetch_market("UNKNOWN")

    assert exc_info.value.status_code == 404
    assert route.call_count == 1


@respx.mock
async def test_no_retry_on_403():
    respx.get(f"{BASE}/markets/0xabc111").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    with pytest.raises(PolymarketHTTPError) as exc_info:
        async with httpx.AsyncClient() as http:
            await _make_adapter(http).fetch_market("0xabc111")

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Network error retry
# ---------------------------------------------------------------------------

@respx.mock
async def test_retry_on_timeout():
    route = respx.get(f"{BASE}/markets/0xabc111")
    route.side_effect = [
        httpx.TimeoutException("timed out"),
        httpx.Response(200, json=MARKET_1),
    ]
    with patch("src.venues.polymarket.client.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).fetch_market("0xabc111")

    assert market.venue_market_id == "0xabc111"
    assert route.call_count == 2


@respx.mock
async def test_raises_after_max_retries_on_network_error():
    respx.get(f"{BASE}/markets/0xabc111").mock(side_effect=httpx.TimeoutException("timed out"))
    with patch("src.venues.polymarket.client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(httpx.TimeoutException):
            async with httpx.AsyncClient() as http:
                await _make_adapter(http).fetch_market("0xabc111")


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------

@respx.mock
async def test_api_key_sent_as_bearer():
    route = respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [], "next_cursor": ""})
    )
    client = PolymarketClient(api_key="test-key-456", _http=None)
    async with httpx.AsyncClient() as http:
        client._injected_http = http
        adapter = PolymarketAdapter(client)
        await adapter.fetch_open_markets()

    assert route.calls[0].request.headers["authorization"] == "Bearer test-key-456"


@respx.mock
async def test_no_auth_header_without_api_key():
    route = respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"data": [], "next_cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        await _make_adapter(http).fetch_open_markets()

    assert "authorization" not in route.calls[0].request.headers
