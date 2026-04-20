"""
Unit tests for src/normalization.py.

No HTTP, no async.  All inputs are constructed directly from domain models.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from src.models.market import Market, Venue
from src.models.orderbook import BookLevel, BookSide, OrderBook
from src.normalization import (
    CanonicalMarket,
    CanonicalSnapshot,
    _canonicalize_source,
    _tokenize,
    normalize_market,
    normalize_snapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc
_TS = datetime(2025, 6, 1, 12, 0, 0, tzinfo=_UTC)


def _market(
    *,
    venue: Venue = Venue.KALSHI,
    market_id: str = "KXFED-25JUN-T5.25",
    question: str = "Will the Fed cut rates by June 2025?",
    resolution_source: str | None = "Federal Reserve",
    category: str | None = "Economy",
    close_time: datetime = datetime(2025, 6, 30, 18, 0, 0, tzinfo=_UTC),
    resolution_time: datetime | None = None,
    is_open: bool = True,
) -> Market:
    return Market(
        venue=venue,
        venue_market_id=market_id,
        question=question,
        question_normalized="",
        resolution_source=resolution_source,
        category=category,
        close_time=close_time,
        resolution_time=resolution_time,
        is_open=is_open,
    )


def _book(
    *,
    market_id: str = "KXFED-25JUN-T5.25",
    outcome: str = "YES",
    ask_price: float = 0.55,
    ask_size: float = 500.0,
    bid_price: float | None = None,
    bid_size: float = 100.0,
    extra_ask_levels: list[tuple[float, float]] | None = None,
    ts: datetime = _TS,
) -> OrderBook:
    asks = [BookLevel(price=ask_price, size=ask_size)]
    if extra_ask_levels:
        asks += [BookLevel(price=p, size=s) for p, s in extra_ask_levels]
    bids = [BookLevel(price=bid_price, size=bid_size)] if bid_price is not None else []
    return OrderBook(
        venue_market_id=market_id,
        outcome=outcome,
        asks=BookSide(levels=asks),
        bids=BookSide(levels=bids),
        snapshot_ts=ts,
    )


# ---------------------------------------------------------------------------
# normalize_market — identity fields
# ---------------------------------------------------------------------------

def test_normalize_market_venue_preserved_kalshi():
    cm = normalize_market(_market(venue=Venue.KALSHI))
    assert cm.venue == Venue.KALSHI


def test_normalize_market_venue_preserved_polymarket():
    cm = normalize_market(_market(venue=Venue.POLYMARKET, market_id="0xabc123"))
    assert cm.venue == Venue.POLYMARKET


def test_normalize_market_venue_market_id_preserved():
    cm = normalize_market(_market(market_id="KXFED-25JUN-T5.25"))
    assert cm.venue_market_id == "KXFED-25JUN-T5.25"


def test_normalize_market_question_preserved():
    cm = normalize_market(_market(question="Will the Fed cut rates by June 2025?"))
    assert cm.question == "Will the Fed cut rates by June 2025?"


# ---------------------------------------------------------------------------
# normalize_market — canonical question text
# ---------------------------------------------------------------------------

def test_normalize_market_question_normalized_lowercased():
    cm = normalize_market(_market(question="Will the Fed cut rates by June 2025?"))
    assert cm.question_normalized == "will the fed cut rates by june 2025"


def test_normalize_market_question_tokens_excludes_stop_words():
    cm = normalize_market(_market(question="Will the Fed cut rates by June 2025?"))
    # "will", "the", "by" are stop words — must not appear
    assert "will" not in cm.question_tokens
    assert "the" not in cm.question_tokens
    assert "by" not in cm.question_tokens


def test_normalize_market_question_tokens_includes_content_words():
    cm = normalize_market(_market(question="Will the Fed cut rates by June 2025?"))
    assert "fed" in cm.question_tokens
    assert "cut" in cm.question_tokens
    assert "rates" in cm.question_tokens
    assert "june" in cm.question_tokens
    assert "2025" in cm.question_tokens


def test_normalize_market_question_tokens_is_frozenset():
    cm = normalize_market(_market())
    assert isinstance(cm.question_tokens, frozenset)


def test_normalize_market_question_tokens_empty_when_all_stop_words():
    cm = normalize_market(_market(question="Will it be or not be?"))
    # "it", "be", "or", "not" are all stop words
    assert cm.question_tokens == frozenset()


def test_normalize_market_question_tokens_polymarket_style():
    cm = normalize_market(_market(
        venue=Venue.POLYMARKET,
        market_id="0xabc",
        question="Will candidate X win the 2025 US election?",
    ))
    assert "candidate" in cm.question_tokens
    assert "x" in cm.question_tokens
    assert "win" in cm.question_tokens
    assert "2025" in cm.question_tokens
    assert "us" in cm.question_tokens
    assert "election" in cm.question_tokens
    assert "the" not in cm.question_tokens


# ---------------------------------------------------------------------------
# normalize_market — canonical outcome mapping
# ---------------------------------------------------------------------------

def test_normalize_market_outcome_yes_is_uppercase():
    cm = normalize_market(_market())
    assert cm.outcome_yes == "YES"


def test_normalize_market_outcome_no_is_uppercase():
    cm = normalize_market(_market())
    assert cm.outcome_no == "NO"


# ---------------------------------------------------------------------------
# normalize_market — canonical resolution source
# ---------------------------------------------------------------------------

def test_normalize_market_source_raw_preserved():
    cm = normalize_market(_market(resolution_source="Federal Reserve"))
    assert cm.resolution_source_raw == "Federal Reserve"


def test_normalize_market_source_federal_reserve():
    assert normalize_market(_market(resolution_source="Federal Reserve")).resolution_source_canonical == "federal reserve"


def test_normalize_market_source_fed_alias():
    assert normalize_market(_market(resolution_source="Fed")).resolution_source_canonical == "federal reserve"


def test_normalize_market_source_fomc_alias():
    assert normalize_market(_market(resolution_source="FOMC")).resolution_source_canonical == "federal reserve"


def test_normalize_market_source_reuters():
    assert normalize_market(_market(resolution_source="Reuters")).resolution_source_canonical == "reuters"


def test_normalize_market_source_ap_alias():
    assert normalize_market(_market(resolution_source="AP")).resolution_source_canonical == "associated press"


def test_normalize_market_source_ap_news_alias():
    assert normalize_market(_market(resolution_source="AP News")).resolution_source_canonical == "associated press"


def test_normalize_market_source_unknown_lowercased():
    cm = normalize_market(_market(resolution_source="SomeObscureSource"))
    assert cm.resolution_source_canonical == "someobscuresource"


def test_normalize_market_source_none_propagates():
    cm = normalize_market(_market(resolution_source=None))
    assert cm.resolution_source_raw is None
    assert cm.resolution_source_canonical is None


def test_normalize_market_source_bls():
    assert normalize_market(_market(resolution_source="Bureau of Labor Statistics")).resolution_source_canonical == "bls"


def test_normalize_market_source_fec():
    assert normalize_market(_market(resolution_source="FEC")).resolution_source_canonical == "fec"


# ---------------------------------------------------------------------------
# normalize_market — canonical close/resolution times
# ---------------------------------------------------------------------------

def test_normalize_market_close_time_utc():
    close = datetime(2025, 6, 30, 18, 0, 0, tzinfo=_UTC)
    cm = normalize_market(_market(close_time=close))
    assert cm.close_time.tzinfo == timezone.utc
    assert cm.close_time == close


def test_normalize_market_close_time_naive_coerced_to_utc():
    naive = datetime(2025, 6, 30, 18, 0, 0)  # no tzinfo
    cm = normalize_market(_market(close_time=naive))
    assert cm.close_time.tzinfo == timezone.utc


def test_normalize_market_close_time_non_utc_converted():
    from datetime import timezone as tz
    eastern = datetime(2025, 6, 30, 14, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    cm = normalize_market(_market(close_time=eastern))
    assert cm.close_time.tzinfo == timezone.utc
    assert cm.close_time.hour == 18  # 14:00 ET = 18:00 UTC


def test_normalize_market_resolution_time_none():
    cm = normalize_market(_market(resolution_time=None))
    assert cm.resolution_time is None


def test_normalize_market_resolution_time_utc_when_set():
    res = datetime(2025, 7, 1, 0, 0, 0, tzinfo=_UTC)
    cm = normalize_market(_market(resolution_time=res))
    assert cm.resolution_time == res
    assert cm.resolution_time.tzinfo == timezone.utc


def test_normalize_market_resolution_time_naive_coerced():
    naive = datetime(2025, 7, 1, 0, 0, 0)
    cm = normalize_market(_market(resolution_time=naive))
    assert cm.resolution_time.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# normalize_market — other fields
# ---------------------------------------------------------------------------

def test_normalize_market_is_open_true():
    assert normalize_market(_market(is_open=True)).is_open is True


def test_normalize_market_is_open_false():
    assert normalize_market(_market(is_open=False)).is_open is False


def test_normalize_market_category_preserved():
    assert normalize_market(_market(category="Economy")).category == "Economy"


def test_normalize_market_category_none():
    assert normalize_market(_market(category=None)).category is None


def test_normalize_market_is_idempotent():
    m = _market()
    assert normalize_market(m) == normalize_market(m)


# ---------------------------------------------------------------------------
# normalize_snapshot — canonical price / depth snapshot
# ---------------------------------------------------------------------------

def test_normalize_snapshot_venue_market_id():
    yes = _book(market_id="KXFED-25JUN-T5.25", outcome="YES")
    no = _book(market_id="KXFED-25JUN-T5.25", outcome="NO", ask_price=0.46)
    snap = normalize_snapshot(yes, no)
    assert snap.venue_market_id == "KXFED-25JUN-T5.25"


def test_normalize_snapshot_venue_kalshi_detected():
    yes = _book(market_id="KXFED-25JUN-T5.25", outcome="YES")
    no = _book(market_id="KXFED-25JUN-T5.25", outcome="NO")
    snap = normalize_snapshot(yes, no)
    assert snap.venue == Venue.KALSHI


def test_normalize_snapshot_venue_polymarket_detected():
    yes = _book(market_id="0xabc123", outcome="YES")
    no = _book(market_id="0xabc123", outcome="NO")
    snap = normalize_snapshot(yes, no)
    assert snap.venue == Venue.POLYMARKET


def test_normalize_snapshot_yes_ask():
    yes = _book(outcome="YES", ask_price=0.55)
    no = _book(outcome="NO", ask_price=0.46)
    snap = normalize_snapshot(yes, no)
    assert snap.yes_ask == pytest.approx(0.55)


def test_normalize_snapshot_no_ask():
    yes = _book(outcome="YES", ask_price=0.55)
    no = _book(outcome="NO", ask_price=0.46)
    snap = normalize_snapshot(yes, no)
    assert snap.no_ask == pytest.approx(0.46)


def test_normalize_snapshot_yes_bid():
    yes = _book(outcome="YES", ask_price=0.55, bid_price=0.53)
    no = _book(outcome="NO", ask_price=0.46)
    snap = normalize_snapshot(yes, no)
    assert snap.yes_bid == pytest.approx(0.53)


def test_normalize_snapshot_no_bid():
    yes = _book(outcome="YES", ask_price=0.55)
    no = _book(outcome="NO", ask_price=0.46, bid_price=0.44)
    snap = normalize_snapshot(yes, no)
    assert snap.no_bid == pytest.approx(0.44)


def test_normalize_snapshot_yes_ask_none_when_empty_book():
    empty = OrderBook(
        venue_market_id="KXFED-25JUN-T5.25",
        outcome="YES",
        asks=BookSide(levels=[]),
        snapshot_ts=_TS,
    )
    no = _book(outcome="NO", ask_price=0.46)
    snap = normalize_snapshot(empty, no)
    assert snap.yes_ask is None


def test_normalize_snapshot_yes_available_at_ask():
    yes = _book(outcome="YES", ask_price=0.55, ask_size=500.0)
    no = _book(outcome="NO", ask_price=0.46, ask_size=300.0)
    snap = normalize_snapshot(yes, no)
    assert snap.yes_available_at_ask == 500.0


def test_normalize_snapshot_no_available_at_ask():
    yes = _book(outcome="YES", ask_price=0.55, ask_size=500.0)
    no = _book(outcome="NO", ask_price=0.46, ask_size=300.0)
    snap = normalize_snapshot(yes, no)
    assert snap.no_available_at_ask == 300.0


def test_normalize_snapshot_yes_total_ask_depth():
    yes = _book(
        outcome="YES",
        ask_price=0.55,
        ask_size=500.0,
        extra_ask_levels=[(0.57, 1000.0)],
    )
    no = _book(outcome="NO", ask_price=0.46)
    snap = normalize_snapshot(yes, no)
    assert snap.yes_total_ask_depth == pytest.approx(1500.0)


def test_normalize_snapshot_no_total_ask_depth():
    yes = _book(outcome="YES", ask_price=0.55)
    no = _book(
        outcome="NO",
        ask_price=0.46,
        ask_size=300.0,
        extra_ask_levels=[(0.48, 800.0)],
    )
    snap = normalize_snapshot(yes, no)
    assert snap.no_total_ask_depth == pytest.approx(1100.0)


def test_normalize_snapshot_ts_from_yes_book():
    ts = datetime(2025, 3, 15, 9, 30, 0, tzinfo=_UTC)
    yes = _book(outcome="YES", ts=ts)
    no = _book(outcome="NO", ts=datetime(2025, 3, 15, 9, 30, 1, tzinfo=_UTC))
    snap = normalize_snapshot(yes, no)
    assert snap.snapshot_ts == ts


def test_normalize_snapshot_ts_coerced_to_utc():
    naive_ts = datetime(2025, 3, 15, 9, 30, 0)
    yes = _book(outcome="YES", ts=naive_ts)
    no = _book(outcome="NO")
    snap = normalize_snapshot(yes, no)
    assert snap.snapshot_ts.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# _tokenize (internal — tested directly for coverage)
# ---------------------------------------------------------------------------

def test_tokenize_basic():
    tokens = _tokenize("will the fed cut rates by june 2025")
    assert tokens == {"fed", "cut", "rates", "june", "2025"}


def test_tokenize_empty_string():
    assert _tokenize("") == frozenset()


def test_tokenize_all_stop_words():
    assert _tokenize("will the be in") == frozenset()


def test_tokenize_returns_frozenset():
    assert isinstance(_tokenize("some question text"), frozenset)


# ---------------------------------------------------------------------------
# _canonicalize_source (internal — tested directly for coverage)
# ---------------------------------------------------------------------------

def test_canonicalize_source_none():
    assert _canonicalize_source(None) is None


def test_canonicalize_source_empty_string():
    assert _canonicalize_source("") is None


def test_canonicalize_source_case_insensitive():
    assert _canonicalize_source("federal reserve") == "federal reserve"
    assert _canonicalize_source("FEDERAL RESERVE") == "federal reserve"
    assert _canonicalize_source("Federal Reserve") == "federal reserve"


def test_canonicalize_source_unknown_lowercased():
    assert _canonicalize_source("MyCustomSource") == "mycustomsource"


def test_canonicalize_source_whitespace_stripped():
    assert _canonicalize_source("  Reuters  ") == "reuters"
