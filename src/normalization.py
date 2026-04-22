"""
Normalization layer: Market + OrderBook → canonical internal representation.

This module is the boundary between the venue adapters (Kalshi, Polymarket) and
the matching / edge-calculation logic.  After passing through here, all data is
venue-agnostic and consistently formatted.

What is normalized
------------------
question text       question_normalized (already lowercased/stripped) + question_tokens
                    (stop-word-removed word set, used by the matcher for overlap scoring)
outcome labels      always uppercase "YES" / "NO"
resolution source   known aliases collapsed to a single canonical name
                    (e.g. "Fed", "FOMC", "Federal Reserve" → "federal reserve")
close/resolution    coerced to UTC; None resolution_time left as None
price / depth       four prices (yes_ask, yes_bid, no_ask, no_bid) in [0,1],
                    available depth at best ask, total ask-side depth, single snapshot_ts
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.models.market import Market, Venue
from src.models.orderbook import OrderBook

# ---------------------------------------------------------------------------
# Stop-word list — removed when building question_tokens
# ---------------------------------------------------------------------------

# Political synonym map — variant forms → canonical token used for Jaccard overlap.
# Applied during tokenization so Poly "Will X be elected..." matches Kalshi "Will X win...".
_POLITICS_SYNONYMS: dict[str, str] = {
    # Win / election verbs
    "wins": "win",
    "winner": "win",
    "elected": "elect",
    "elect": "elect",
    "reelected": "reelect",
    "re-elected": "reelect",
    # Office titles
    "presidential": "president",
    "presidency": "president",
    "senator": "senate",
    "senatorial": "senate",
    "congressional": "congress",
    "congressman": "congress",
    "congresswoman": "congress",
    "representative": "congress",
    "representatives": "congress",
    "gubernatorial": "governor",
    # Parties (collapse to root)
    "gop": "republican",
    "democratic": "democrat",
    "dem": "democrat",
    "dems": "democrat",
    # Legislative actions
    "approved": "approve",
    "approval": "approve",
    "passed": "pass",
    "passing": "pass",
    "vetoed": "veto",
    "signed": "sign",
    "signing": "sign",
    "confirmed": "confirm",
    "confirmation": "confirm",
    "impeached": "impeach",
    "impeachment": "impeach",
    # Misc political
    "majority": "majority",
    "minority": "minority",
    "control": "control",
    "controls": "control",
    "controlled": "control",
}

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "by",
        "do", "does", "for", "from", "had", "has", "have", "if",
        "in", "is", "it", "its", "no", "not", "of", "on", "or",
        "so", "than", "that", "the", "this", "to", "up", "was",
        "were", "will", "with", "yes",
    }
)

# ---------------------------------------------------------------------------
# Resolution-source alias table  (lowercase key → canonical lowercase value)
# ---------------------------------------------------------------------------

_SOURCE_ALIASES: dict[str, str] = {
    # Federal Reserve
    "federal reserve": "federal reserve",
    "fed": "federal reserve",
    "fomc": "federal reserve",
    "federal open market committee": "federal reserve",
    # News wires
    "reuters": "reuters",
    "reuters news agency": "reuters",
    "associated press": "associated press",
    "ap news": "associated press",
    "ap": "associated press",
    # US government
    "fec": "fec",
    "federal election commission": "fec",
    "sec": "sec",
    "securities and exchange commission": "sec",
    "cdc": "cdc",
    "centers for disease control": "cdc",
    "centers for disease control and prevention": "cdc",
    "bls": "bls",
    "bureau of labor statistics": "bls",
    "bea": "bea",
    "bureau of economic analysis": "bea",
    "cms": "cms",
    "centers for medicare and medicaid services": "cms",
    # Sports
    "nba": "nba",
    "nfl": "nfl",
    "mlb": "mlb",
    "nhl": "nhl",
    "fifa": "fifa",
    "ufc": "ufc",
    "espn": "espn",
    # Crypto / finance
    "coinmarketcap": "coinmarketcap",
    "coingecko": "coingecko",
    "deribit": "deribit",
    # Venues (self-referential)
    "polymarket": "polymarket",
    "kalshi": "kalshi",
}


# ---------------------------------------------------------------------------
# Canonical dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalMarket:
    """Venue-agnostic normalized market for matching and downstream logic."""

    venue: Venue
    venue_market_id: str

    question: str                       # original question text from the venue
    question_normalized: str            # lowercase, punctuation stripped
    question_tokens: frozenset[str]     # content words only (stop words removed)

    outcome_yes: str                    # always "YES"
    outcome_no: str                     # always "NO"

    resolution_source_raw: str | None   # as returned by the venue
    resolution_source_canonical: str | None  # aliased + lowercased

    category: str | None                # as returned by the venue

    close_time: datetime                # UTC-aware
    resolution_time: datetime | None    # UTC-aware, or None

    is_open: bool


@dataclass(frozen=True)
class CanonicalSnapshot:
    """Unified price and depth view for one binary market across both outcome sides."""

    venue: Venue
    venue_market_id: str

    yes_ask: float | None     # best ask price for YES, in [0, 1]
    yes_bid: float | None     # best bid price for YES, in [0, 1]
    no_ask: float | None      # best ask price for NO,  in [0, 1]
    no_bid: float | None      # best bid price for NO,  in [0, 1]

    yes_available_at_ask: float    # contracts available at the best YES ask
    no_available_at_ask: float     # contracts available at the best NO ask

    yes_total_ask_depth: float     # total ask-side depth for YES
    no_total_ask_depth: float      # total ask-side depth for NO

    snapshot_ts: datetime          # UTC timestamp of the YES book (both books captured near-simultaneously)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_market(market: Market) -> CanonicalMarket:
    """Convert a domain Market into a CanonicalMarket.

    Idempotent: calling twice on the same Market returns an equal result.
    """
    return CanonicalMarket(
        venue=market.venue,
        venue_market_id=market.venue_market_id,
        question=market.question,
        question_normalized=market.question_normalized,
        question_tokens=_tokenize(market.question_normalized),
        outcome_yes="YES",
        outcome_no="NO",
        resolution_source_raw=market.resolution_source,
        resolution_source_canonical=_canonicalize_source(market.resolution_source),
        category=market.category,
        close_time=_to_utc(market.close_time),
        resolution_time=_to_utc(market.resolution_time) if market.resolution_time else None,
        is_open=market.is_open,
    )


def normalize_snapshot(
    yes_book: OrderBook,
    no_book: OrderBook,
) -> CanonicalSnapshot:
    """Convert a pair of OrderBooks (YES + NO) into a CanonicalSnapshot.

    Both books must belong to the same market; venue_market_id is taken from yes_book.
    snapshot_ts is taken from yes_book (both are captured near-simultaneously).
    """
    return CanonicalSnapshot(
        venue=_venue_from_book(yes_book),
        venue_market_id=yes_book.venue_market_id,
        yes_ask=yes_book.best_ask,
        yes_bid=yes_book.best_bid,
        no_ask=no_book.best_ask,
        no_bid=no_book.best_bid,
        yes_available_at_ask=yes_book.available_at_ask(),
        no_available_at_ask=no_book.available_at_ask(),
        yes_total_ask_depth=yes_book.asks.total_available,
        no_total_ask_depth=no_book.asks.total_available,
        snapshot_ts=_to_utc(yes_book.snapshot_ts),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _tokenize(question_normalized: str) -> frozenset[str]:
    """Split a normalized question into content words, discarding stop words.

    Political synonym folding is applied so "elected"→"elect", "gop"→"republican", etc.
    """
    tokens: set[str] = set()
    for word in question_normalized.split():
        if not word or word in _STOP_WORDS:
            continue
        tokens.add(_POLITICS_SYNONYMS.get(word, word))
    return frozenset(tokens)


def _canonicalize_source(raw: str | None) -> str | None:
    """Map a raw resolution source string to a canonical name.

    Known aliases (case-insensitive) are collapsed to a single lowercase name.
    Unknown sources are returned lowercased and stripped.
    Returns None when raw is None or empty.
    """
    if not raw:
        return None
    key = raw.lower().strip()
    return _SOURCE_ALIASES.get(key, key)


def _to_utc(dt: datetime) -> datetime:
    """Coerce a datetime to UTC.  Naive datetimes are assumed to already be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _venue_from_book(book: OrderBook) -> Venue:
    """Infer venue from the first character of venue_market_id.

    Convention:
      - Kalshi tickers start with a letter and are all-caps short codes
      - Polymarket condition_ids start with "0x" (hex)

    Falls back to POLYMARKET for unrecognized formats.
    """
    mid = book.venue_market_id
    if mid.startswith("0x") or mid.startswith("0X"):
        return Venue.POLYMARKET
    return Venue.KALSHI
