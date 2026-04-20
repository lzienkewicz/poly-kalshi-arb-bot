"""
Kalshi API response parsing — the ONLY module that knows Kalshi field names.

Layout
------
1. Raw intermediate dataclasses  (KalshiMarketRaw, KalshiOrderbookRaw)
2. Extraction functions          (extract_market, extract_orderbook)
   — map raw dict keys → typed intermediates, with safe defaults
3. Conversion functions          (to_market, to_orderbooks, to_orderbooks_from_market)
   — intermediates → shared domain models

If Kalshi renames or restructures a field, only extraction functions need updating.
Conversion functions stay stable because they consume the typed intermediates.

Kalshi orderbook convention (v2 trade API):
  "yes" array = ask levels for YES (prices at which you can BUY YES), best first
  "no"  array = ask levels for NO  (prices at which you can BUY NO),  best first
  Prices are integers in cents (0-100). Sizes are integer contract counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.models.market import Market, Venue
from src.models.orderbook import BookLevel, BookSide, OrderBook


# ---------------------------------------------------------------------------
# Intermediate dataclasses — typed bridge between raw JSON and domain models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KalshiMarketRaw:
    """All Kalshi market fields the bot cares about, extracted from the API payload."""

    ticker: str
    title: str
    status: str                      # lowercase, e.g. "open" / "closed"
    close_time_str: str              # raw ISO-8601 string
    market_type: str                 # e.g. "binary"
    yes_ask_cents: int | None        # integer cents 0-100, or None if absent
    no_ask_cents: int | None
    yes_bid_cents: int | None
    no_bid_cents: int | None
    category: str | None
    resolution_source: str | None    # best-effort from settlement_sources
    expiration_time_str: str | None
    raw: dict[str, Any]              # original payload kept for audit / debug


@dataclass(frozen=True)
class KalshiOrderbookRaw:
    """Orderbook depth extracted from the Kalshi orderbook endpoint."""

    ticker: str
    # Each entry is (price_cents, size_contracts). Sorted best-first by the API.
    yes_levels: list[tuple[int, int]]
    no_levels: list[tuple[int, int]]


# ---------------------------------------------------------------------------
# Extraction helpers — all raw-dict key accesses live here
# ---------------------------------------------------------------------------

def _int_opt(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_opt(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _resolution_source(raw: dict[str, Any]) -> str | None:
    """Pull the first settlement source name from the API payload."""
    sources = raw.get("settlement_sources") or []
    if isinstance(sources, list) and sources:
        first = sources[0]
        if isinstance(first, dict):
            return _str_opt(first.get("name") or first.get("url"))
    # Fall back to free-text rules field if present
    return _str_opt(raw.get("rules_primary"))


def extract_market(raw: dict[str, Any]) -> KalshiMarketRaw:
    """Extract all relevant fields from a raw Kalshi market API payload.

    This function is the single point of truth for Kalshi field names.
    Callers receive a typed intermediate; they never touch raw dict keys.
    """
    return KalshiMarketRaw(
        ticker=str(raw["ticker"]),
        title=str(raw.get("title") or raw.get("subtitle") or ""),
        status=str(raw.get("status", "")).lower(),
        close_time_str=str(raw["close_time"]),
        market_type=str(raw.get("market_type", "")).lower(),
        yes_ask_cents=_int_opt(raw.get("yes_ask")),
        no_ask_cents=_int_opt(raw.get("no_ask")),
        yes_bid_cents=_int_opt(raw.get("yes_bid")),
        no_bid_cents=_int_opt(raw.get("no_bid")),
        category=_str_opt(raw.get("category")),
        resolution_source=_resolution_source(raw),
        expiration_time_str=_str_opt(raw.get("expiration_time")),
        raw=raw,
    )


def extract_orderbook(raw: dict[str, Any], ticker: str) -> KalshiOrderbookRaw:
    """Extract orderbook depth from a raw Kalshi orderbook API payload."""

    def _parse_levels(side: Any) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for entry in side or []:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                try:
                    result.append((int(entry[0]), int(entry[1])))
                except (TypeError, ValueError):
                    continue  # skip malformed levels silently
        return result

    ob = raw.get("orderbook") or raw
    return KalshiOrderbookRaw(
        ticker=ticker,
        yes_levels=_parse_levels(ob.get("yes")),
        no_levels=_parse_levels(ob.get("no")),
    )


# ---------------------------------------------------------------------------
# Conversion — intermediates → domain models
# ---------------------------------------------------------------------------

def cents_to_dollars(cents: int) -> float:
    return cents / 100.0


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_market(parsed: KalshiMarketRaw) -> Market:
    """Convert a KalshiMarketRaw intermediate to the shared Market model."""
    close_time = _parse_dt(parsed.close_time_str)
    resolution_time = _parse_dt(parsed.expiration_time_str) if parsed.expiration_time_str else None

    return Market(
        venue=Venue.KALSHI,
        venue_market_id=parsed.ticker,
        question=parsed.title,
        question_normalized="",   # auto-derived by Market's field validator
        close_time=close_time,
        resolution_time=resolution_time,
        is_open=(parsed.status == "open"),
        resolution_source=parsed.resolution_source,
        category=parsed.category,
        raw=parsed.raw,
    )


def _build_ask_side(levels: list[tuple[int, int]]) -> BookSide:
    """Build a BookSide from (price_cents, size) pairs. Sorted ascending (best ask first)."""
    book_levels = [
        BookLevel(price=cents_to_dollars(p), size=float(s))
        for p, s in levels
        if 0 <= p <= 100
    ]
    book_levels.sort(key=lambda bl: bl.price)
    return BookSide(levels=book_levels)


def _build_bid_side(levels: list[tuple[int, int]]) -> BookSide:
    """Build a BookSide from (price_cents, size) pairs. Sorted descending (best bid first)."""
    book_levels = [
        BookLevel(price=cents_to_dollars(p), size=float(s))
        for p, s in levels
        if 0 <= p <= 100
    ]
    book_levels.sort(key=lambda bl: bl.price, reverse=True)
    return BookSide(levels=book_levels)


def to_orderbooks(
    parsed: KalshiOrderbookRaw,
    snapshot_ts: datetime,
) -> tuple[OrderBook, OrderBook]:
    """Convert KalshiOrderbookRaw to (YES OrderBook, NO OrderBook).

    Convention: Kalshi's yes/no arrays are ASK levels for the respective outcome
    (the prices at which you can BUY YES or BUY NO). Sizes are contract counts.
    """
    yes_book = OrderBook(
        venue_market_id=parsed.ticker,
        outcome="YES",
        asks=_build_ask_side(parsed.yes_levels),
        snapshot_ts=snapshot_ts,
    )
    no_book = OrderBook(
        venue_market_id=parsed.ticker,
        outcome="NO",
        asks=_build_ask_side(parsed.no_levels),
        snapshot_ts=snapshot_ts,
    )
    return yes_book, no_book


def to_orderbooks_from_market(
    parsed: KalshiMarketRaw,
    snapshot_ts: datetime,
) -> tuple[OrderBook, OrderBook]:
    """Build single-level OrderBooks from top-of-book market fields.

    Used when only the price is needed (no depth check). Size is unknown (0.0).
    """
    yes_ask_levels: list[tuple[int, int]] = (
        [(parsed.yes_ask_cents, 0)] if parsed.yes_ask_cents is not None else []
    )
    no_ask_levels: list[tuple[int, int]] = (
        [(parsed.no_ask_cents, 0)] if parsed.no_ask_cents is not None else []
    )
    yes_bid_levels: list[tuple[int, int]] = (
        [(parsed.yes_bid_cents, 0)] if parsed.yes_bid_cents is not None else []
    )
    no_bid_levels: list[tuple[int, int]] = (
        [(parsed.no_bid_cents, 0)] if parsed.no_bid_cents is not None else []
    )

    yes_book = OrderBook(
        venue_market_id=parsed.ticker,
        outcome="YES",
        asks=_build_ask_side(yes_ask_levels),
        bids=_build_bid_side(yes_bid_levels),
        snapshot_ts=snapshot_ts,
    )
    no_book = OrderBook(
        venue_market_id=parsed.ticker,
        outcome="NO",
        asks=_build_ask_side(no_ask_levels),
        bids=_build_bid_side(no_bid_levels),
        snapshot_ts=snapshot_ts,
    )
    return yes_book, no_book
