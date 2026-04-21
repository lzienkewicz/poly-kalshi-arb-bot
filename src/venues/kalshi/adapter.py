"""
Public-facing Kalshi adapter.

Wires the HTTP client to the parser and exposes the three operations the bot needs:
  - fetch_open_markets()    — full market discovery with pagination
  - fetch_market(ticker)    — single market refresh
  - fetch_orderbooks(ticker)— depth snapshot for the depth check
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from src.models.market import Market
from src.models.orderbook import OrderBook
from src.venues.kalshi._parser import (
    extract_market,
    extract_orderbook,
    to_market,
    to_orderbooks,
    to_orderbooks_from_market,
)
from src.venues.kalshi.client import KalshiClient

log = structlog.get_logger(__name__)

_MAX_PAGES = 20          # safety cap on pagination loops
_ORDERBOOK_DEPTH = 10    # levels to request from the depth endpoint


class KalshiAdapter:
    """Normalizes Kalshi API responses into shared domain models.

    Parameters
    ----------
    client:
        Configured KalshiClient.  Caller owns the lifecycle (context manager).
    """

    def __init__(self, client: KalshiClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def fetch_open_markets(self) -> list[Market]:
        """Return all currently open Kalshi binary markets.

        Handles cursor-based pagination transparently.
        Non-binary or non-open entries are silently filtered.
        Malformed individual records are logged and skipped.
        """
        markets: list[Market] = []
        cursor: str | None = None

        for page in range(_MAX_PAGES):
            raw = await self._client.get_markets(cursor=cursor)
            raw_markets: list[dict] = raw.get("markets") or []

            for raw_m in raw_markets:
                try:
                    parsed = extract_market(raw_m)
                    if parsed.status not in ("open", "active"):
                        continue
                    if parsed.market_type and parsed.market_type != "binary":
                        continue
                    markets.append(to_market(parsed))
                except (KeyError, ValueError, TypeError) as exc:
                    log.warning(
                        "kalshi_market_parse_skip",
                        ticker=raw_m.get("ticker"),
                        error=str(exc),
                    )

            cursor = raw.get("cursor") or ""
            if not cursor:
                log.debug("kalshi_market_pages_done", pages=page + 1)
                break

        log.info("kalshi_markets_fetched", count=len(markets))
        return markets

    # ------------------------------------------------------------------
    # Single-market refresh
    # ------------------------------------------------------------------

    async def fetch_market(self, ticker: str) -> Market:
        """Fetch and normalize a single Kalshi market by ticker."""
        raw = await self._client.get_market(ticker)
        raw_m = raw.get("market") or raw
        parsed = extract_market(raw_m)
        market = to_market(parsed)
        log.debug("kalshi_market_fetched", ticker=ticker, is_open=market.is_open)
        return market

    # ------------------------------------------------------------------
    # Orderbook depth (for depth check, Section 8 of spec)
    # ------------------------------------------------------------------

    async def fetch_orderbooks(self, ticker: str) -> tuple[OrderBook, OrderBook]:
        """Return (YES OrderBook, NO OrderBook) with full depth for ticker.

        Raises KalshiHTTPError on unrecoverable API errors.
        """
        snapshot_ts = datetime.now(timezone.utc)
        raw = await self._client.get_orderbook(ticker, depth=_ORDERBOOK_DEPTH)
        parsed_ob = extract_orderbook(raw, ticker)
        yes_book, no_book = to_orderbooks(parsed_ob, snapshot_ts)

        log.debug(
            "kalshi_orderbook_fetched",
            ticker=ticker,
            yes_ask=yes_book.best_ask,
            no_ask=no_book.best_ask,
            yes_depth=yes_book.asks.total_available,
            no_depth=no_book.asks.total_available,
        )
        return yes_book, no_book

    # ------------------------------------------------------------------
    # Top-of-book only (price check without depth; no extra HTTP call)
    # ------------------------------------------------------------------

    async def fetch_top_of_book(self, ticker: str) -> tuple[OrderBook, OrderBook]:
        """Return (YES OrderBook, NO OrderBook) built from market-level top-of-book fields.

        Cheaper than fetch_orderbooks — requires only one API call and no depth
        endpoint hit.  Size at ask is 0.0 (unknown); do not use for depth checks.
        """
        snapshot_ts = datetime.now(timezone.utc)
        raw = await self._client.get_market(ticker)
        raw_m = raw.get("market") or raw
        parsed = extract_market(raw_m)
        yes_book, no_book = to_orderbooks_from_market(parsed, snapshot_ts)

        log.debug(
            "kalshi_top_of_book_fetched",
            ticker=ticker,
            yes_ask=yes_book.best_ask,
            no_ask=no_book.best_ask,
        )
        return yes_book, no_book
