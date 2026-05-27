"""
Unit tests for src/paper_trader.py.

No HTTP, no async.  All inputs constructed directly from domain models.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models.event_pair import EventPair, MatchConfidence, MatchStatus
from src.models.market import Market, Venue
from src.models.opportunity import Opportunity, OpportunityStatus, TradeDirection
from src.models.orderbook import BookLevel, BookSide, OrderBook
from src.paper_trader import PaperTrade, PaperTrader, RiskLimits

_UTC = timezone.utc
_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=_UTC)
_FRESH = _NOW - timedelta(seconds=5)
_FAR_FUTURE = datetime(2099, 1, 1, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(venue: Venue, mid: str, question: str = "Test market?") -> Market:
    return Market(
        venue=venue,
        venue_market_id=mid,
        question=question,
        question_normalized="",
        close_time=_FAR_FUTURE,
    )


def _book(mid: str, outcome: str, ask: float, bid: float, size: float = 500.0) -> OrderBook:
    return OrderBook(
        venue_market_id=mid,
        outcome=outcome,
        asks=BookSide(levels=[BookLevel(price=ask, size=size)]),
        bids=BookSide(levels=[BookLevel(price=bid, size=size)]),
        snapshot_ts=_FRESH,
    )


def _pair(poly_mid: str = "0xabc", kalshi_mid: str = "KXTEST") -> EventPair:
    return EventPair(
        polymarket=_market(Venue.POLYMARKET, poly_mid),
        kalshi=_market(Venue.KALSHI, kalshi_mid),
        status=MatchStatus.MATCHED,
        confidence=MatchConfidence.EXACT,
    )


def _tradeable_opp(
    pair: EventPair | None = None,
    poly_ask: float = 0.40,
    kalshi_ask: float = 0.40,
    poly_size: float = 500.0,
    kalshi_size: float = 500.0,
    exec_size: float = 10.0,
    kalshi_fee: float = 0.02,
    slip_per_leg: float = 0.005,
) -> Opportunity:
    if pair is None:
        pair = _pair()
    gross = 1.0 - (poly_ask + kalshi_ask)
    fee = kalshi_ask * kalshi_fee
    slip = 2 * slip_per_leg
    net = round(gross - fee - slip, 8)
    poly_mid = pair.polymarket.venue_market_id
    kalshi_mid = pair.kalshi.venue_market_id
    return Opportunity(
        pair=pair,
        poly_book=_book(poly_mid, "YES", ask=poly_ask, bid=poly_ask - 0.02, size=poly_size),
        kalshi_book=_book(kalshi_mid, "NO", ask=kalshi_ask, bid=kalshi_ask - 0.02, size=kalshi_size),
        evaluated_at=_NOW,
        direction=TradeDirection.A,
        poly_ask=poly_ask,
        kalshi_ask=kalshi_ask,
        gross_edge=gross,
        fee_estimate=fee,
        slippage_buffer=slip,
        stale_penalty=0.0,
        net_edge=net,
        executable_size_usdc=exec_size,
        status=OpportunityStatus.TRADEABLE,
    )


def _rejected_opp(pair: EventPair | None = None) -> Opportunity:
    if pair is None:
        pair = _pair()
    poly_mid = pair.polymarket.venue_market_id
    kalshi_mid = pair.kalshi.venue_market_id
    return Opportunity(
        pair=pair,
        poly_book=_book(poly_mid, "YES", ask=0.49, bid=0.47),
        kalshi_book=_book(kalshi_mid, "NO", ask=0.49, bid=0.47),
        evaluated_at=_NOW,
        direction=TradeDirection.A,
        poly_ask=0.49,
        kalshi_ask=0.49,
        gross_edge=0.02,
        fee_estimate=0.49 * 0.02,
        slippage_buffer=0.01,
        stale_penalty=0.0,
        net_edge=round(0.02 - 0.49 * 0.02 - 0.01, 8),
        executable_size_usdc=10.0,
        status=OpportunityStatus.REJECTED_EDGE,
        fail_reason="BELOW_MIN_EDGE",
    )


def _trader(tmp_path: Path, limits: RiskLimits | None = None) -> PaperTrader:
    return PaperTrader(db_path=tmp_path / "trades.jsonl", limits=limits)


# ---------------------------------------------------------------------------
# RiskLimits defaults
# ---------------------------------------------------------------------------

def test_risk_limits_defaults() -> None:
    lim = RiskLimits()
    assert lim.max_position_per_market_usdc == 50.0
    assert lim.max_total_exposure_usdc == 200.0
    assert lim.min_liquidity_per_leg_usdc == 5.0
    assert lim.cooldown_s == 300.0


# ---------------------------------------------------------------------------
# can_enter — rejection scenarios
# ---------------------------------------------------------------------------

def test_can_enter_rejects_non_tradeable(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    opp = _rejected_opp()
    ok, reason = trader.can_enter(opp)
    assert not ok
    assert "not_tradeable" in reason


def test_can_enter_allows_tradeable(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    opp = _tradeable_opp()
    ok, reason = trader.can_enter(opp)
    assert ok
    assert reason == ""


def test_can_enter_cooldown(tmp_path: Path) -> None:
    limits = RiskLimits(cooldown_s=9999.0)
    trader = _trader(tmp_path, limits)
    opp = _tradeable_opp()
    # First execute plants the cooldown timestamp
    trade = trader.execute(opp)
    assert trade is not None

    # Second attempt with a different Opportunity object for same pair key
    opp2 = _tradeable_opp()
    ok, reason = trader.can_enter(opp2)
    assert not ok
    # After the first trade, the pair has an open position → duplicate check fires first
    assert "duplicate_open_position" in reason or "cooldown" in reason


def test_can_enter_duplicate_open_position(tmp_path: Path) -> None:
    trader = _trader(tmp_path, RiskLimits(cooldown_s=0.0))
    opp = _tradeable_opp()
    trader.execute(opp)

    # The same pair key now has an open position
    opp2 = _tradeable_opp()
    ok, reason = trader.can_enter(opp2)
    assert not ok
    assert "duplicate_open_position" in reason


def test_can_enter_market_cap(tmp_path: Path) -> None:
    limits = RiskLimits(max_position_per_market_usdc=15.0, cooldown_s=0.0)
    trader = _trader(tmp_path, limits)
    opp = _tradeable_opp(exec_size=10.0)
    trader.execute(opp)

    # Now open position = 10, cap = 15, next would add another 10 → 20 > 15
    pair2 = _pair(poly_mid="0xdifferent", kalshi_mid="KXOTHER")
    opp2 = _tradeable_opp(pair=pair2, exec_size=10.0)
    ok2, reason2 = trader.can_enter(opp2)
    # Different pair, so no duplicate or cooldown issue. Portfolio is fine (10 < 200).
    assert ok2

    # Same pair, would push to 20 > 15 — but this is blocked first by duplicate position.
    opp3 = _tradeable_opp(exec_size=10.0)
    ok3, reason3 = trader.can_enter(opp3)
    assert not ok3
    assert "duplicate_open_position" in reason3


def test_can_enter_portfolio_cap(tmp_path: Path) -> None:
    limits = RiskLimits(max_total_exposure_usdc=15.0, cooldown_s=0.0)
    trader = _trader(tmp_path, limits)

    opp1 = _tradeable_opp(_pair("0xa", "KXA"), exec_size=10.0)
    trader.execute(opp1)

    # Second pair: total would be 10 + 10 = 20 > 15
    opp2 = _tradeable_opp(_pair("0xb", "KXB"), exec_size=10.0)
    ok, reason = trader.can_enter(opp2)
    assert not ok
    assert "portfolio_cap_exceeded" in reason


def test_can_enter_poly_liquidity_too_low(tmp_path: Path) -> None:
    limits = RiskLimits(min_liquidity_per_leg_usdc=100.0)
    trader = _trader(tmp_path, limits)
    # 0.40 ask × 5 contracts = $2 liquidity < $100 min
    opp = _tradeable_opp(poly_size=5.0)
    ok, reason = trader.can_enter(opp)
    assert not ok
    assert "poly_insufficient_liquidity" in reason


def test_can_enter_kalshi_liquidity_too_low(tmp_path: Path) -> None:
    limits = RiskLimits(min_liquidity_per_leg_usdc=100.0)
    trader = _trader(tmp_path, limits)
    # poly has plenty, kalshi has only 5 contracts × 0.40 = $2 < $100 min
    opp = _tradeable_opp(poly_size=10000.0, kalshi_size=5.0)
    ok, reason = trader.can_enter(opp)
    assert not ok
    assert "kalshi_insufficient_liquidity" in reason


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def test_execute_returns_trade(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    opp = _tradeable_opp()
    trade = trader.execute(opp)
    assert trade is not None
    assert trade.status == "open"
    assert trade.direction == "A"
    assert trade.poly_side == "YES"
    assert trade.kalshi_side == "NO"
    assert trade.poly_fill_price == pytest.approx(0.40, abs=1e-6)
    assert trade.kalshi_fill_price == pytest.approx(0.40, abs=1e-6)
    assert trade.net_edge == pytest.approx(opp.net_edge, abs=1e-5)
    assert trade.estimated_profit_usdc == pytest.approx(opp.estimated_profit_usdc, abs=1e-4)


def test_execute_returns_none_when_rejected(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    opp = _rejected_opp()
    trade = trader.execute(opp)
    assert trade is None
    assert trader.all_trades() == []


def test_execute_populates_in_memory_state(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    opp = _tradeable_opp()
    trader.execute(opp)
    assert len(trader.open_trades()) == 1
    assert len(trader.all_trades()) == 1


# ---------------------------------------------------------------------------
# Persistence (JSONL round-trip)
# ---------------------------------------------------------------------------

def test_execute_persists_to_disk(tmp_path: Path) -> None:
    db = tmp_path / "trades.jsonl"
    trader = PaperTrader(db_path=db)
    trader.execute(_tradeable_opp())

    assert db.exists()
    lines = [l for l in db.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["status"] == "open"
    assert data["poly_side"] == "YES"


def test_load_restores_trades_on_init(tmp_path: Path) -> None:
    db = tmp_path / "trades.jsonl"
    # First session: execute and persist
    t1 = PaperTrader(db_path=db)
    t1.execute(_tradeable_opp(_pair("0xa1", "KXA1")))
    t1.execute(_tradeable_opp(_pair("0xa2", "KXA2")))

    # Second session: load from disk
    t2 = PaperTrader(db_path=db)
    assert len(t2.all_trades()) == 2
    assert len(t2.open_trades()) == 2


def test_load_skips_malformed_lines(tmp_path: Path) -> None:
    db = tmp_path / "trades.jsonl"
    db.write_text('{"bad": "data"}\n{"also_bad": true}\n')
    # Should not raise; malformed entries are skipped
    trader = PaperTrader(db_path=db)
    assert len(trader.all_trades()) == 0


def test_new_db_starts_empty(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    assert trader.all_trades() == []
    assert trader.open_trades() == []


# ---------------------------------------------------------------------------
# portfolio_summary
# ---------------------------------------------------------------------------

def test_portfolio_summary_empty(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    summary = trader.portfolio_summary()
    assert summary["total_trades_all_time"] == 0
    assert summary["open_positions"] == 0
    assert summary["total_deployed_usdc"] == 0.0
    assert summary["hypothetical_pnl_usdc"] == 0.0


def test_portfolio_summary_after_trade(tmp_path: Path) -> None:
    trader = _trader(tmp_path)
    opp = _tradeable_opp()
    trade = trader.execute(opp)
    assert trade is not None

    summary = trader.portfolio_summary()
    assert summary["total_trades_all_time"] == 1
    assert summary["open_positions"] == 1
    assert summary["total_deployed_usdc"] == pytest.approx(10.0)
    assert summary["hypothetical_pnl_usdc"] == pytest.approx(trade.estimated_profit_usdc, abs=1e-4)


def test_portfolio_summary_multiple_pairs(tmp_path: Path) -> None:
    limits = RiskLimits(cooldown_s=0.0)
    trader = _trader(tmp_path, limits)
    for i in range(3):
        trader.execute(_tradeable_opp(_pair(f"0x{i:04x}", f"KX{i:04X}")))

    summary = trader.portfolio_summary()
    assert summary["total_trades_all_time"] == 3
    assert summary["open_positions"] == 3
    assert summary["total_deployed_usdc"] == pytest.approx(30.0)
