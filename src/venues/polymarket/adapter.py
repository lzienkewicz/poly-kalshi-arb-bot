"""
Public-facing Polymarket adapter.

Wires the HTTP client to the parser and exposes the operations the bot needs:
  - fetch_open_markets()                         — paginated market discovery
  - fetch_market(condition_id)                   — single market refresh
  - fetch_orderbooks(condition_id,               — full depth snapshot (2 HTTP calls)
                     yes_token_id, no_token_id)
  - fetch_top_of_book(condition_id,              — same endpoint; no cheaper price-only
                      yes_token_id, no_token_id)   call exists on the CLOB API

Note: unlike Kalshi, Polymarket orderbooks are per-token rather than per-market, so
callers must supply both token IDs (available from Market.raw["tokens"]).
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from src.models.market import Market
from src.models.orderbook import OrderBook
from src.venues.polymarket._parser import (
    extract_book,
    extract_market,
    to_market,
    to_orderbook,
)
from src.venues.polymarket.client import PolymarketClient

log = structlog.get_logger(__name__)

_MAX_PAGES = 50
_PAGE_SIZE = 100


class PolymarketAdapter:
    """Normalizes Polymarket CLOB API responses into shared domain models.

    Parameters
    ----------
    client:
        Configured PolymarketClient.  Caller owns the lifecycle (context manager).
    """

    def __init__(self, client: PolymarketClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def fetch_open_markets(self) -> list[Market]:
        """Return all currently active Polymarket binary markets with future deadlines.

        Handles cursor-based pagination transparently.
        Non-active entries and markets whose effective deadline is already in the
        past are silently filtered.  Malformed individual records are logged and
        skipped.
        """
        now = datetime.now(timezone.utc)
        markets: list[Market] = []
        raw_count = 0

        for page in range(_MAX_PAGES):
            offset = page * _PAGE_SIZE
            raw_markets = await self._client.get_markets(offset=offset, limit=_PAGE_SIZE)
            if not raw_markets:
                log.debug("polymarket_market_pages_done", pages=page)
                break
            raw_count += len(raw_markets)

            for raw_m in raw_markets:
                try:
                    parsed = extract_market(raw_m)
                    if parsed.status != "active":
                        continue
                    market = to_market(parsed)
                    # Reject markets whose effective deadline has already passed.
                    # close_time is game_start_time for sports, end_date_iso otherwise.
                    if market.close_time <= now:
                        log.debug(
                            "polymarket_market_past_deadline_skip",
                            condition_id=parsed.condition_id,
                            close_time=market.close_time.isoformat(),
                        )
                        continue
                    markets.append(market)
                except (KeyError, ValueError, TypeError) as exc:
                    log.warning(
                        "polymarket_market_parse_skip",
                        condition_id=raw_m.get("condition_id") or raw_m.get("conditionId"),
                        error=str(exc),
                    )

            if len(raw_markets) < _PAGE_SIZE:
                log.debug("polymarket_market_pages_done", pages=page + 1)
                break

        log.info(
            "polymarket_markets_fetched",
            raw_count=raw_count,
            kept=len(markets),
        )
        return markets

    # ------------------------------------------------------------------
    # Single-market refresh
    # ------------------------------------------------------------------

    async def fetch_market(self, condition_id: str) -> Market:
        """Fetch and normalize a single Polymarket market by condition_id."""
        raw = await self._client.get_market(condition_id)
        parsed = extract_market(raw)
        market = to_market(parsed)
        log.debug("polymarket_market_fetched", condition_id=condition_id, is_open=market.is_open)
        return market

    # ------------------------------------------------------------------
    # Orderbook depth
    # ------------------------------------------------------------------

    async def fetch_orderbooks(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> tuple[OrderBook, OrderBook]:
        """Return (YES OrderBook, NO OrderBook) with full depth.

        Makes two HTTP calls — one per token side.

        Raises PolymarketHTTPError on unrecoverable API errors.
        """
        snapshot_ts = datetime.now(timezone.utc)

        yes_raw = await self._client.get_book(yes_token_id)
        no_raw = await self._client.get_book(no_token_id)

        yes_book = to_orderbook(
            extract_book(yes_raw, condition_id, yes_token_id, "YES"),
            snapshot_ts,
        )
        no_book = to_orderbook(
            extract_book(no_raw, condition_id, no_token_id, "NO"),
            snapshot_ts,
        )

        log.debug(
            "polymarket_orderbooks_fetched",
            condition_id=condition_id,
            yes_ask=yes_book.best_ask,
            no_ask=no_book.best_ask,
            yes_depth=yes_book.asks.total_available,
            no_depth=no_book.asks.total_available,
        )
        return yes_book, no_book

    # ------------------------------------------------------------------
    # Top-of-book (reuses fetch_orderbooks — no cheaper CLOB endpoint)
    # ------------------------------------------------------------------

    async def fetch_top_of_book(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> tuple[OrderBook, OrderBook]:
        """Return (YES OrderBook, NO OrderBook) at top-of-book only.

        The Polymarket CLOB has no dedicated price-only endpoint, so this
        delegates to fetch_orderbooks.  Use it when you want the same interface
        as the Kalshi adapter but don't need to distinguish depth from price.
        """
        return await self.fetch_orderbooks(condition_id, yes_token_id, no_token_id)
