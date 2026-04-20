from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.opportunity import Opportunity, TradeDirection
from src.models.position import Position, PositionLeg, PositionStatus
from tests.models.conftest import make_position_id


def _make_position(
    tradeable_opportunity: Opportunity,
    poly_leg: PositionLeg,
    kalshi_leg: PositionLeg,
    **overrides: object,
) -> Position:
    defaults: dict[str, object] = {
        "position_id": make_position_id(),
        "mode": "paper",
        "opportunity": tradeable_opportunity,
        "poly_leg": poly_leg,
        "kalshi_leg": kalshi_leg,
        "direction": TradeDirection.A,
        "opened_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return Position(**defaults)  # type: ignore[arg-type]


def test_position_creates(
    tradeable_opportunity: Opportunity,
    poly_leg: PositionLeg,
    kalshi_leg: PositionLeg,
):
    p = _make_position(tradeable_opportunity, poly_leg, kalshi_leg)
    assert p.mode == "paper"
    assert p.status == PositionStatus.OPEN
    assert p.gross_pnl_usdc is None
    assert p.net_pnl_usdc is None
    assert p.settled_at is None


def test_position_default_status_open(
    tradeable_opportunity: Opportunity,
    poly_leg: PositionLeg,
    kalshi_leg: PositionLeg,
):
    p = _make_position(tradeable_opportunity, poly_leg, kalshi_leg)
    assert p.status == PositionStatus.OPEN
    assert p.is_settled is False


def test_position_total_cost(
    tradeable_opportunity: Opportunity,
    poly_leg: PositionLeg,
    kalshi_leg: PositionLeg,
):
    p = _make_position(tradeable_opportunity, poly_leg, kalshi_leg)
    assert p.total_cost_usdc == pytest.approx(20.0)


def test_position_total_fees(
    tradeable_opportunity: Opportunity,
    poly_leg: PositionLeg,
    kalshi_leg: PositionLeg,
):
    p = _make_position(tradeable_opportunity, poly_leg, kalshi_leg)
    assert p.total_fees_usdc == pytest.approx(0.20)


def test_position_settled_via_copy(
    tradeable_opportunity: Opportunity,
    poly_leg: PositionLeg,
    kalshi_leg: PositionLeg,
):
    p = _make_position(tradeable_opportunity, poly_leg, kalshi_leg)
    settled = p.model_copy(
        update={
            "status": PositionStatus.SETTLED,
            "settled_at": datetime.now(timezone.utc),
            "winning_side": "YES",
            "gross_pnl_usdc": 0.30,
            "net_pnl_usdc": 0.10,
        }
    )
    assert settled.is_settled is True
    assert settled.net_pnl_usdc == pytest.approx(0.10)
    assert p.status == PositionStatus.OPEN  # original unchanged


def test_position_is_frozen(
    tradeable_opportunity: Opportunity,
    poly_leg: PositionLeg,
    kalshi_leg: PositionLeg,
):
    p = _make_position(tradeable_opportunity, poly_leg, kalshi_leg)
    with pytest.raises(Exception):
        p.status = PositionStatus.SETTLED  # type: ignore[misc]


def test_position_leg_price_bounds():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PositionLeg(
            venue="polymarket",
            venue_market_id="x",
            side="YES",
            fill_price=1.01,
            contracts=10.0,
            notional_usdc=10.0,
            fee_paid_usdc=0.0,
        )


def test_position_leg_is_simulated_default():
    leg = PositionLeg(
        venue="kalshi",
        venue_market_id="x",
        side="NO",
        fill_price=0.45,
        contracts=22.0,
        notional_usdc=10.0,
        fee_paid_usdc=0.20,
    )
    assert leg.is_simulated is True
