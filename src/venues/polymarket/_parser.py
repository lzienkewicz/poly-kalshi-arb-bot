"""
Polymarket CLOB API response parsing — the ONLY module that knows Polymarket field names.

Layout
------
1. Raw intermediate dataclasses  (PolymarketMarketRaw, PolymarketBookRaw)
2. Extraction functions          (extract_market, extract_book)
   — map raw dict keys → typed intermediates, with safe defaults
3. Conversion functions          (to_market, to_orderbook)
   — intermediates → shared domain models

If Polymarket renames or restructures a field, only extraction functions need updating.

Polymarket CLOB conventions:
  - Prices are decimals in [0.0, 1.0] (not cents)
  - Asks are sorted ascending (best ask = lowest price, first)
  - Bids are sorted descending (best bid = highest price, first)
  - Each binary market has two tokens: YES token_id and NO token_id
  - Orderbooks are per-token, not per-condition
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
class PolymarketMarketRaw:
    """All Polymarket market fields the bot cares about, extracted from the CLOB API payload."""

    condition_id: str
    question: str
    status: str               # "active" or "closed"
    end_date_iso: str | None  # raw ISO-8601 close time, or None
    category: str | None
    description: str | None
    yes_token_id: str | None  # token_id for the YES side
    no_token_id: str | None   # token_id for the NO side
    neg_risk: bool            # True for neg-risk (complementary) markets
    raw: dict[str, Any]       # original payload kept for audit / debug


@dataclass(frozen=True)
class PolymarketBookRaw:
    """Orderbook depth extracted from the Polymarket CLOB /book endpoint."""

    condition_id: str
    token_id: str
    outcome: str                         # "YES" or "NO"
    bids: list[tuple[float, float]]      # (price, size), sorted descending
    asks: list[tuple[float, float]]      # (price, size), sorted ascending


# ---------------------------------------------------------------------------
# Extraction helpers — all raw-dict key accesses live here
# ---------------------------------------------------------------------------

def _str_opt(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _extract_tokens(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (yes_token_id, no_token_id) from the tokens array."""
    tokens = raw.get("tokens") or []
    yes_id: str | None = None
    no_id: str | None = None
    for token in tokens:
        if isinstance(token, dict):
            outcome = str(token.get("outcome", "")).lower()
            tid = _str_opt(token.get("token_id"))
            if outcome == "yes":
                yes_id = tid
            elif outcome == "no":
                no_id = tid
    return yes_id, no_id


def _market_status(raw: dict[str, Any]) -> str:
    """Derive a normalized status string from CLOB market fields."""
    if raw.get("closed") is True:
        return "closed"
    if raw.get("active") is True:
        return "active"
    return str(raw.get("status", "")).lower() or "unknown"


def extract_market(raw: dict[str, Any]) -> PolymarketMarketRaw:
    """Extract all relevant fields from a raw Polymarket CLOB market payload.

    This function is the single point of truth for Polymarket field names.
    Callers receive a typed intermediate; they never touch raw dict keys.
    """
    yes_token_id, no_token_id = _extract_tokens(raw)
    return PolymarketMarketRaw(
        condition_id=str(raw["condition_id"]),
        question=str(raw.get("question") or ""),
        status=_market_status(raw),
        end_date_iso=_str_opt(raw.get("end_date_iso")),
        category=_str_opt(raw.get("category")),
        description=_str_opt(raw.get("description")),
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        neg_risk=bool(raw.get("neg_risk", False)),
        raw=raw,
    )


def _parse_book_levels(levels: Any) -> list[tuple[float, float]]:
    """Parse a list of {price, size} dicts into (float, float) tuples."""
    result: list[tuple[float, float]] = []
    for entry in levels or []:
        if isinstance(entry, dict):
            try:
                price = float(entry["price"])
                size = float(entry["size"])
                result.append((price, size))
            except (KeyError, TypeError, ValueError):
                continue  # skip malformed levels silently
    return result


def extract_book(
    raw: dict[str, Any],
    condition_id: str,
    token_id: str,
    outcome: str,
) -> PolymarketBookRaw:
    """Extract orderbook depth from a raw Polymarket CLOB /book endpoint payload."""
    return PolymarketBookRaw(
        condition_id=condition_id,
        token_id=token_id,
        outcome=outcome.upper(),
        bids=_parse_book_levels(raw.get("bids")),
        asks=_parse_book_levels(raw.get("asks")),
    )


# ---------------------------------------------------------------------------
# Conversion — intermediates → domain models
# ---------------------------------------------------------------------------

def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_market(parsed: PolymarketMarketRaw) -> Market:
    """Convert a PolymarketMarketRaw intermediate to the shared Market model."""
    close_time = (
        _parse_dt(parsed.end_date_iso)
        if parsed.end_date_iso
        else datetime(9999, 12, 31, tzinfo=timezone.utc)
    )
    return Market(
        venue=Venue.POLYMARKET,
        venue_market_id=parsed.condition_id,
        question=parsed.question,
        question_normalized="",   # auto-derived by Market's field validator
        close_time=close_time,
        is_open=(parsed.status == "active"),
        resolution_source=None,
        category=parsed.category,
        raw=parsed.raw,
    )


def _build_ask_side(levels: list[tuple[float, float]]) -> BookSide:
    """Build an ask BookSide, sorted ascending (lowest price first = best ask)."""
    book_levels = [
        BookLevel(price=p, size=s)
        for p, s in levels
        if 0.0 <= p <= 1.0
    ]
    book_levels.sort(key=lambda bl: bl.price)
    return BookSide(levels=book_levels)


def _build_bid_side(levels: list[tuple[float, float]]) -> BookSide:
    """Build a bid BookSide, sorted descending (highest price first = best bid)."""
    book_levels = [
        BookLevel(price=p, size=s)
        for p, s in levels
        if 0.0 <= p <= 1.0
    ]
    book_levels.sort(key=lambda bl: bl.price, reverse=True)
    return BookSide(levels=book_levels)


def to_orderbook(parsed: PolymarketBookRaw, snapshot_ts: datetime) -> OrderBook:
    """Convert a PolymarketBookRaw intermediate to the shared OrderBook model."""
    return OrderBook(
        venue_market_id=parsed.condition_id,
        outcome=parsed.outcome,
        bids=_build_bid_side(parsed.bids),
        asks=_build_ask_side(parsed.asks),
        snapshot_ts=snapshot_ts,
    )
