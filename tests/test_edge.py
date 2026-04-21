"""
Unit tests for src/edge.py.

No HTTP, no async. All inputs constructed directly from domain models.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.edge import EdgeConfig, calculate_edge
from src.models.event_pair import EventPair, MatchConfidence, MatchStatus
from src.models.market import Market, Venue
from src.models.opportunity import OpportunityStatus, TradeDirection
from src.models.orderbook import BookLevel, BookSide, OrderBook
from src.normalization import CanonicalSnapshot

_UTC = timezone.utc
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=_UTC)
_FRESH = _NOW - timedelta(seconds=5)    # 5 s old — fresh
_STALE = _NOW - timedelta(seconds=60)   # 60 s old — stale


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_CFG = EdgeConfig(
    poly_fee=0.0,
    kalshi_fee=0.02,
    slippage_per_leg=0.005,
    stale_penalty_per_leg=0.005,
    max_price_age_s=30.0,
    net_edge_min=0.02,
    trade_size_usdc=10.0,
)


def _snap(
    *,
    venue: Venue = Venue.POLYMARKET,
    yes_ask: float | None = 0.45,
    no_ask: float | None = 0.45,
    yes_bid: float | None = None,
    no_bid: float | None = None,
    yes_avail: float = 500.0,
    no_avail: float = 500.0,
    ts: datetime = _FRESH,
) -> CanonicalSnapshot:
    mid = "0xabc" if venue == Venue.POLYMARKET else "KXTEST"
    return CanonicalSnapshot(
        venue=venue,
        venue_market_id=mid,
        yes_ask=yes_ask,
        yes_bid=yes_bid,
        no_ask=no_ask,
        no_bid=no_bid,
        yes_available_at_ask=yes_avail,
        no_available_at_ask=no_avail,
        yes_total_ask_depth=yes_avail,
        no_total_ask_depth=no_avail,
        snapshot_ts=ts,
    )


def _book(
    venue: Venue = Venue.POLYMARKET,
    outcome: str = "YES",
    ask: float = 0.45,
    size: float = 500.0,
) -> OrderBook:
    mid = "0xabc" if venue == Venue.POLYMARKET else "KXTEST"
    return OrderBook(
        venue_market_id=mid,
        outcome=outcome,
        asks=BookSide(levels=[BookLevel(price=ask, size=size)]),
        snapshot_ts=_FRESH,
    )


def _market(venue: Venue, mid: str, question: str = "Will X happen?") -> Market:
    return Market(
        venue=venue,
        venue_market_id=mid,
        question=question,
        question_normalized="",
        close_time=datetime(2025, 12, 31, tzinfo=_UTC),
    )


def _pair() -> EventPair:
    return EventPair(
        polymarket=_market(Venue.POLYMARKET, "0xabc"),
        kalshi=_market(Venue.KALSHI, "KXTEST"),
        status=MatchStatus.MATCHED,
        confidence=MatchConfidence.EXACT,
    )


def _books():
    """Return (poly_yes, kalshi_no, poly_no, kalshi_yes) OrderBooks."""
    return (
        _book(Venue.POLYMARKET, "YES"),
        _book(Venue.KALSHI, "NO"),
        _book(Venue.POLYMARKET, "NO"),
        _book(Venue.KALSHI, "YES"),
    )


def _calc(poly_snap, kalshi_snap, cfg=_CFG):
    py, kn, pn, ky = _books()
    return calculate_edge(poly_snap, kalshi_snap, _pair(), py, kn, pn, ky, cfg, now=_NOW)


# ---------------------------------------------------------------------------
# Direction A formula
# ---------------------------------------------------------------------------

def test_direction_a_selected_when_better():
    # poly YES cheap, kalshi NO cheap → Direction A wins
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.45, no_ask=0.60)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.60, no_ask=0.45)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.A


def test_direction_b_selected_when_better():
    # poly NO cheap, kalshi YES cheap → Direction B wins
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.60, no_ask=0.45)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.45, no_ask=0.60)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.B


def test_gross_edge_direction_a_formula():
    # Direction A: gross = 1 - (yes_ask*(1+fee) + slip) - (no_ask*(1+fee) + slip)
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.A
    expected_gross = 1.0 - (0.40 * 1.0 + 0.005) - (0.40 * 1.02 + 0.005)
    assert opp.gross_edge == pytest.approx(expected_gross, abs=1e-9)


def test_gross_edge_direction_b_formula():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.99, no_ask=0.40)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.40, no_ask=0.99)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.B
    expected_gross = 1.0 - (0.40 * 1.0 + 0.005) - (0.40 * 1.02 + 0.005)
    assert opp.gross_edge == pytest.approx(expected_gross, abs=1e-9)


def test_net_edge_subtracts_fee_slippage_penalty():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40)
    opp = _calc(poly, kalshi)
    expected_net = opp.gross_edge - opp.fee_estimate - opp.slippage_buffer - opp.stale_penalty
    assert opp.net_edge == pytest.approx(expected_net, abs=1e-9)


# ---------------------------------------------------------------------------
# TRADEABLE path
# ---------------------------------------------------------------------------

def test_tradeable_when_sufficient_edge_and_depth():
    # gross = 1 - (0.40 * 1.0 + 0.005) - (0.40 * 1.02 + 0.005) ≈ 0.182 >> 0.02
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.status == OpportunityStatus.TRADEABLE
    assert opp.fail_reason is None


def test_tradeable_estimated_profit():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.estimated_profit_usdc == pytest.approx(opp.net_edge * 10.0, abs=1e-9)


def test_poly_side_direction_a():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.poly_side == "YES"
    assert opp.kalshi_side == "NO"


def test_poly_side_direction_b():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.poly_side == "NO"
    assert opp.kalshi_side == "YES"


# ---------------------------------------------------------------------------
# REJECTED_EDGE
# ---------------------------------------------------------------------------

def test_rejected_edge_when_net_edge_below_min():
    # Both asks at 0.50 → gross ≈ 1 - (0.50 + 0.005) - (0.51 + 0.005) = -0.02
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.50, no_ask=0.50)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.50, no_ask=0.50)
    opp = _calc(poly, kalshi)
    assert opp.status == OpportunityStatus.REJECTED_EDGE


def test_rejected_edge_reason_code():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.50, no_ask=0.50)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.50, no_ask=0.50)
    opp = _calc(poly, kalshi)
    assert opp.fail_reason == "BELOW_MIN_EDGE"


def test_rejected_edge_at_exact_threshold():
    # Craft prices so net_edge is just below 0.02
    # gross_A = 1 - (p*(1.0)+0.005) - (k*1.02+0.005) = 1 - p - 0.005 - 1.02k - 0.005
    # With p=k=0.475: gross = 1 - 0.475 - 0.005 - 0.4845 - 0.005 = 0.0305
    # fee = 0*0.475 + 0.02*0.475 = 0.0095; slip = 0.01; net = 0.0305 - 0.0095 - 0.01 = 0.011
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.475, no_ask=0.99)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.475)
    opp = _calc(poly, kalshi)
    assert opp.net_edge < _CFG.net_edge_min
    assert opp.status == OpportunityStatus.REJECTED_EDGE


def test_rejected_edge_no_asks():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=None, no_ask=None)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=None, no_ask=None)
    opp = _calc(poly, kalshi)
    assert opp.status == OpportunityStatus.REJECTED_EDGE
    assert opp.fail_reason == "NO_ASKS"


# ---------------------------------------------------------------------------
# REJECTED_STALE
# ---------------------------------------------------------------------------

def test_rejected_stale_poly_stale():
    # Good gross edge but poly data is stale → stale penalty kills net edge
    # gross_A ≈ 0.182 (from 0.40/0.40 asks), penalty = 0.005 per stale leg
    # pre-penalty net ≈ 0.182 - fee - slip ≈ 0.164 >> 0.02
    # post-penalty net ≈ 0.164 - 0.005 = 0.159 >> 0.02 still — need bigger penalty
    # Use a tighter edge: gross ≈ 0.025, penalty = 0.01 (both stale)
    # p=0.475, k=0.475: gross ≈ 0.0305, pre-stale net ≈ 0.011 < 0.02 already
    # Need edge that's ≥ 0.02 pre-penalty but < 0.02 after one stale leg
    # pre-stale net = 0.025, penalty = 0.005 → post = 0.020 (still passes)
    # penalty = 0.010 → post = 0.015 < 0.020 ✓
    cfg = EdgeConfig(
        poly_fee=0.0,
        kalshi_fee=0.0,
        slippage_per_leg=0.0,
        stale_penalty_per_leg=0.015,
        max_price_age_s=30.0,
        net_edge_min=0.02,
        trade_size_usdc=10.0,
    )
    # gross_A = 1 - 0.455 - 0.455 = 0.09; no fees/slip → pre-stale net = 0.09; post = 0.09-0.015=0.075 still ok
    # Use very thin edge: p=0.49, k=0.49 → gross = 0.02; pre-stale = 0.02; post = 0.005 < 0.02
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.49, no_ask=0.99, ts=_STALE)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.49, ts=_FRESH)
    opp = calculate_edge(poly, kalshi, _pair(), *_books(), cfg, now=_NOW)
    assert opp.status == OpportunityStatus.REJECTED_STALE
    assert opp.poly_book_stale is True
    assert opp.kalshi_book_stale is False


def test_rejected_stale_both_stale():
    cfg = EdgeConfig(
        poly_fee=0.0, kalshi_fee=0.0, slippage_per_leg=0.0,
        stale_penalty_per_leg=0.015, max_price_age_s=30.0,
        net_edge_min=0.02, trade_size_usdc=10.0,
    )
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.49, no_ask=0.99, ts=_STALE)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.49, ts=_STALE)
    opp = calculate_edge(poly, kalshi, _pair(), *_books(), cfg, now=_NOW)
    assert opp.status == OpportunityStatus.REJECTED_STALE
    assert opp.poly_book_stale is True
    assert opp.kalshi_book_stale is True
    assert opp.stale_penalty == pytest.approx(0.030)


def test_stale_penalty_does_not_apply_when_fresh():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0, ts=_FRESH)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0, ts=_FRESH)
    opp = _calc(poly, kalshi)
    assert opp.stale_penalty == pytest.approx(0.0)
    assert opp.status == OpportunityStatus.TRADEABLE


# ---------------------------------------------------------------------------
# REJECTED_DEPTH
# ---------------------------------------------------------------------------

def test_rejected_depth_poly_insufficient():
    # trade_size=10, poly YES ask=0.40 → need 25 contracts; give 5 → reject
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=5.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.status == OpportunityStatus.REJECTED_DEPTH
    assert opp.fail_reason == "POLY_DEPTH_INSUFFICIENT"


def test_rejected_depth_kalshi_insufficient():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=5.0)
    opp = _calc(poly, kalshi)
    assert opp.status == OpportunityStatus.REJECTED_DEPTH
    assert opp.fail_reason == "KALSHI_DEPTH_INSUFFICIENT"


def test_depth_check_uses_correct_leg_for_direction_b():
    # Direction B: poly NO, kalshi YES
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.99, no_ask=0.40, no_avail=5.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.B
    assert opp.status == OpportunityStatus.REJECTED_DEPTH
    assert opp.fail_reason == "POLY_DEPTH_INSUFFICIENT"


def test_depth_exactly_sufficient_passes():
    # need exactly 25 contracts (10 / 0.40), provide exactly 25
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=25.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.status == OpportunityStatus.TRADEABLE


def test_depth_zero_available_rejected():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=0.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.status == OpportunityStatus.REJECTED_DEPTH


# ---------------------------------------------------------------------------
# Fee and slippage arithmetic
# ---------------------------------------------------------------------------

def test_zero_kalshi_fee_no_fee_estimate():
    cfg = EdgeConfig(poly_fee=0.0, kalshi_fee=0.0, slippage_per_leg=0.0,
                     stale_penalty_per_leg=0.0, max_price_age_s=30.0,
                     net_edge_min=0.02, trade_size_usdc=10.0)
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = calculate_edge(poly, kalshi, _pair(), *_books(), cfg, now=_NOW)
    assert opp.fee_estimate == pytest.approx(0.0)
    assert opp.slippage_buffer == pytest.approx(0.0)
    assert opp.gross_edge == pytest.approx(opp.net_edge)


def test_kalshi_fee_applied_to_kalshi_ask_only():
    cfg = EdgeConfig(poly_fee=0.0, kalshi_fee=0.02, slippage_per_leg=0.0,
                     stale_penalty_per_leg=0.0, max_price_age_s=30.0,
                     net_edge_min=0.0, trade_size_usdc=10.0)
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = calculate_edge(poly, kalshi, _pair(), *_books(), cfg, now=_NOW)
    assert opp.fee_estimate == pytest.approx(0.40 * 0.02)  # kalshi_ask * kalshi_fee


def test_slippage_applied_to_both_legs():
    cfg = EdgeConfig(poly_fee=0.0, kalshi_fee=0.0, slippage_per_leg=0.005,
                     stale_penalty_per_leg=0.0, max_price_age_s=30.0,
                     net_edge_min=0.0, trade_size_usdc=10.0)
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = calculate_edge(poly, kalshi, _pair(), *_books(), cfg, now=_NOW)
    assert opp.slippage_buffer == pytest.approx(0.01)  # 2 * 0.005


# ---------------------------------------------------------------------------
# Opportunity model invariant (net_edge formula validated by Pydantic)
# ---------------------------------------------------------------------------

def test_opportunity_net_edge_invariant_holds():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    computed = opp.gross_edge - opp.fee_estimate - opp.slippage_buffer - opp.stale_penalty
    assert opp.net_edge == pytest.approx(computed, abs=1e-9)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_direction_a_chosen_on_tie():
    # Equal gross edges → Direction A wins (A >= B condition)
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.40, yes_avail=1000.0, no_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.40, no_ask=0.40, yes_avail=1000.0, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.A


def test_only_direction_a_possible_when_no_ask_missing():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=None, yes_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=None, no_ask=0.40, no_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.A


def test_only_direction_b_possible_when_yes_ask_missing():
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=None, no_ask=0.40, no_avail=1000.0)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.40, no_ask=None, yes_avail=1000.0)
    opp = _calc(poly, kalshi)
    assert opp.direction == TradeDirection.B


def test_now_injectable_for_staleness():
    # Both snaps at _FRESH (5 s old relative to _NOW)
    # If we pass now = _FRESH + 40s → snaps are 40 s old → stale
    future_now = _FRESH + timedelta(seconds=40)
    poly = _snap(venue=Venue.POLYMARKET, yes_ask=0.40, no_ask=0.99, ts=_FRESH)
    kalshi = _snap(venue=Venue.KALSHI, yes_ask=0.99, no_ask=0.40, ts=_FRESH)
    opp = calculate_edge(poly, kalshi, _pair(), *_books(), _CFG, now=future_now)
    assert opp.poly_book_stale is True
    assert opp.kalshi_book_stale is True
