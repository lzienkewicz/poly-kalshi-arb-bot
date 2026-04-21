"""
Integration tests for KalshiClient + KalshiAdapter using respx HTTP mocks.

All HTTP calls are intercepted — no real network traffic.
asyncio.sleep is patched to a no-op in retry tests so the suite stays fast.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from src.models.market import Venue
from src.venues.kalshi.adapter import KalshiAdapter
from src.venues.kalshi.client import KalshiClient, KalshiHTTPError

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ---------------------------------------------------------------------------
# Shared payloads
# ---------------------------------------------------------------------------

MARKET_1 = {
    "ticker": "KXFED-25JUN-T5.25",
    "title": "Will the Fed cut rates by June 2025?",
    "status": "open",
    "close_time": "2025-06-30T18:00:00Z",
    "market_type": "binary",
    "yes_ask": 55,
    "no_ask": 46,
    "yes_bid": 53,
    "no_bid": 44,
    "category": "Economy",
    "settlement_sources": [{"name": "Federal Reserve"}],
}

MARKET_2 = {
    "ticker": "KXELEC-25NOV",
    "title": "Will candidate X win the 2025 election?",
    "status": "open",
    "close_time": "2025-11-05T00:00:00Z",
    "market_type": "binary",
    "yes_ask": 62,
    "no_ask": 39,
    "yes_bid": 60,
    "no_bid": 37,
    "category": "Politics",
    "settlement_sources": [],
}

ORDERBOOK_PAYLOAD = {
    "orderbook": {
        "yes": [[55, 500], [57, 1000]],
        "no": [[46, 300], [48, 800]],
    }
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(http: httpx.AsyncClient) -> KalshiAdapter:
    client = KalshiClient(_http=http, max_attempts=3)
    return KalshiAdapter(client)


# ---------------------------------------------------------------------------
# fetch_open_markets — single page
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_open_markets_returns_markets():
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [MARKET_1, MARKET_2], "cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        adapter = _make_adapter(http)
        markets = await adapter.fetch_open_markets()

    assert len(markets) == 2
    assert markets[0].venue_market_id == "KXFED-25JUN-T5.25"
    assert markets[1].venue_market_id == "KXELEC-25NOV"


@respx.mock
async def test_fetch_open_markets_venue_is_kalshi():
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [MARKET_1], "cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert markets[0].venue == Venue.KALSHI


@respx.mock
async def test_fetch_open_markets_filters_closed():
    closed = {**MARKET_1, "status": "closed"}
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [closed, MARKET_2], "cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1
    assert markets[0].venue_market_id == "KXELEC-25NOV"


@respx.mock
async def test_fetch_open_markets_filters_non_binary():
    scalar = {**MARKET_1, "market_type": "scalar"}
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [scalar, MARKET_2], "cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1
    assert markets[0].venue_market_id == "KXELEC-25NOV"


# ---------------------------------------------------------------------------
# fetch_open_markets — pagination
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_open_markets_follows_cursor():
    route = respx.get(f"{BASE}/markets")
    route.side_effect = [
        httpx.Response(200, json={"markets": [MARKET_1], "cursor": "page2"}),
        httpx.Response(200, json={"markets": [MARKET_2], "cursor": ""}),
    ]
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 2
    assert route.called


@respx.mock
async def test_fetch_open_markets_stops_on_empty_cursor():
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [MARKET_1], "cursor": None})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    assert len(markets) == 1


# ---------------------------------------------------------------------------
# fetch_open_markets — malformed records
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_open_markets_skips_malformed_records():
    malformed = {"status": "open"}  # missing required ticker and close_time
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [malformed, MARKET_1], "cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        markets = await _make_adapter(http).fetch_open_markets()

    # malformed skipped, MARKET_1 returned
    assert len(markets) == 1
    assert markets[0].venue_market_id == "KXFED-25JUN-T5.25"


# ---------------------------------------------------------------------------
# fetch_market — single market
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_market_returns_market():
    respx.get(f"{BASE}/markets/KXFED-25JUN-T5.25").mock(
        return_value=httpx.Response(200, json={"market": MARKET_1})
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).fetch_market("KXFED-25JUN-T5.25")

    assert market.venue_market_id == "KXFED-25JUN-T5.25"
    assert market.is_open is True


@respx.mock
async def test_fetch_market_question_normalized():
    respx.get(f"{BASE}/markets/KXFED-25JUN-T5.25").mock(
        return_value=httpx.Response(200, json={"market": MARKET_1})
    )
    async with httpx.AsyncClient() as http:
        market = await _make_adapter(http).fetch_market("KXFED-25JUN-T5.25")

    assert market.question_normalized == "will the fed cut rates by june 2025"


# ---------------------------------------------------------------------------
# fetch_orderbooks
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_orderbooks_returns_yes_no():
    respx.get(f"{BASE}/markets/KXFED-25JUN-T5.25/orderbook").mock(
        return_value=httpx.Response(200, json=ORDERBOOK_PAYLOAD)
    )
    async with httpx.AsyncClient() as http:
        yes_book, no_book = await _make_adapter(http).fetch_orderbooks("KXFED-25JUN-T5.25")

    assert yes_book.outcome == "YES"
    assert no_book.outcome == "NO"
    assert yes_book.best_ask == pytest.approx(0.55)
    assert no_book.best_ask == pytest.approx(0.46)


@respx.mock
async def test_fetch_orderbooks_depth_available():
    respx.get(f"{BASE}/markets/KXFED-25JUN-T5.25/orderbook").mock(
        return_value=httpx.Response(200, json=ORDERBOOK_PAYLOAD)
    )
    async with httpx.AsyncClient() as http:
        yes_book, _ = await _make_adapter(http).fetch_orderbooks("KXFED-25JUN-T5.25")

    assert yes_book.available_at_ask() == 500.0


@respx.mock
async def test_fetch_orderbooks_depth_param_sent():
    route = respx.get(f"{BASE}/markets/KXFED-25JUN-T5.25/orderbook").mock(
        return_value=httpx.Response(200, json=ORDERBOOK_PAYLOAD)
    )
    async with httpx.AsyncClient() as http:
        await _make_adapter(http).fetch_orderbooks("KXFED-25JUN-T5.25")

    assert "depth" in str(route.calls[0].request.url)


# ---------------------------------------------------------------------------
# fetch_top_of_book
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_top_of_book_prices():
    respx.get(f"{BASE}/markets/KXFED-25JUN-T5.25").mock(
        return_value=httpx.Response(200, json={"market": MARKET_1})
    )
    async with httpx.AsyncClient() as http:
        yes_book, no_book = await _make_adapter(http).fetch_top_of_book("KXFED-25JUN-T5.25")

    assert yes_book.best_ask == pytest.approx(0.55)
    assert no_book.best_ask == pytest.approx(0.46)
    assert yes_book.asks.levels[0].size == 0.0  # size unknown from market data


# ---------------------------------------------------------------------------
# Retry — 5xx
# ---------------------------------------------------------------------------

@respx.mock
async def test_retry_succeeds_after_500():
    route = respx.get(f"{BASE}/markets/X")
    route.side_effect = [
        httpx.Response(500, json={"error": "internal"}),
        httpx.Response(200, json={"market": MARKET_1}),
    ]
    with patch("src.venues.kalshi.client.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).fetch_market("X")

    assert market.venue_market_id == "KXFED-25JUN-T5.25"
    assert route.call_count == 2


@respx.mock
async def test_raises_after_max_retries_on_500():
    respx.get(f"{BASE}/markets/X").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )
    with patch("src.venues.kalshi.client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(KalshiHTTPError) as exc_info:
            async with httpx.AsyncClient() as http:
                await _make_adapter(http).fetch_market("X")

    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Retry — 429 rate limit
# ---------------------------------------------------------------------------

@respx.mock
async def test_retry_on_429_respects_retry_after():
    route = respx.get(f"{BASE}/markets/X")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "0.1"}, json={}),
        httpx.Response(200, json={"market": MARKET_1}),
    ]
    sleep_mock = AsyncMock()
    with patch("src.venues.kalshi.client.asyncio.sleep", sleep_mock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).fetch_market("X")

    assert market.venue_market_id == "KXFED-25JUN-T5.25"
    sleep_mock.assert_called_once_with(pytest.approx(0.1, abs=0.01))


# ---------------------------------------------------------------------------
# No retry on 4xx client errors
# ---------------------------------------------------------------------------

@respx.mock
async def test_no_retry_on_404():
    route = respx.get(f"{BASE}/markets/UNKNOWN").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    with pytest.raises(KalshiHTTPError) as exc_info:
        async with httpx.AsyncClient() as http:
            await _make_adapter(http).fetch_market("UNKNOWN")

    assert exc_info.value.status_code == 404
    assert route.call_count == 1  # never retried


@respx.mock
async def test_no_retry_on_403():
    respx.get(f"{BASE}/markets/X").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    with pytest.raises(KalshiHTTPError) as exc_info:
        async with httpx.AsyncClient() as http:
            await _make_adapter(http).fetch_market("X")

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Network error retry
# ---------------------------------------------------------------------------

@respx.mock
async def test_retry_on_timeout():
    route = respx.get(f"{BASE}/markets/X")
    route.side_effect = [
        httpx.TimeoutException("timed out"),
        httpx.Response(200, json={"market": MARKET_1}),
    ]
    with patch("src.venues.kalshi.client.asyncio.sleep", new_callable=AsyncMock):
        async with httpx.AsyncClient() as http:
            market = await _make_adapter(http).fetch_market("X")

    assert market.venue_market_id == "KXFED-25JUN-T5.25"
    assert route.call_count == 2


@respx.mock
async def test_raises_after_max_retries_on_network_error():
    respx.get(f"{BASE}/markets/X").mock(side_effect=httpx.TimeoutException("timed out"))
    with patch("src.venues.kalshi.client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(httpx.TimeoutException):
            async with httpx.AsyncClient() as http:
                await _make_adapter(http).fetch_market("X")


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------

@respx.mock
async def test_api_key_sent_as_bearer():
    route = respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [], "cursor": ""})
    )
    client = KalshiClient(api_key="test-key-123", _http=None)
    async with httpx.AsyncClient() as http:
        client._injected_http = http
        adapter = KalshiAdapter(client)
        await adapter.fetch_open_markets()

    assert route.calls[0].request.headers["authorization"] == "Bearer test-key-123"


@respx.mock
async def test_no_auth_header_without_api_key():
    route = respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [], "cursor": ""})
    )
    async with httpx.AsyncClient() as http:
        await _make_adapter(http).fetch_open_markets()

    assert "authorization" not in route.calls[0].request.headers
