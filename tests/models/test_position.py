from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.models.market import Side
from src.models.opportunity import Direction
from src.models.position import Position, PositionStatus, TradingMode


def _make_position(**overrides: object) -> Position:
    defaults: dict[str, object] = {
        "ts_entered": datetime.now(timezone.utc),
        "mode": TradingMode.PAPER,
        "question": "Will the Fed cut rates in June?",
        "poly_market_id": "cond_abc123",
        "kalshi_market_id": "FED-JUNE-CUT",
        "direction": Direction.A,
        "poly_side": Side.YES,
        "kalshi_side": Side.NO,
        "poly_price": 0.55,
        "kalshi_price": 0.45,
        "trade_size_usdc": 10.0,
        "net_edge_at_entry": 0.03,
    }
    defaults.update(overrides)
    return Position(**defaults)  # type: ignore[arg-type]


def test_position_creates():
    p = _make_position()
    assert p.mode == TradingMode.PAPER
    assert p.status == PositionStatus.OPEN
    assert p.pnl is None
    assert p.ts_settled is None


def test_position_auto_uuid():
    p1 = _make_position()
    p2 = _make_position()
    assert isinstance(p1.id, UUID)
    assert p1.id != p2.id


def test_position_default_status_is_open():
    p = _make_position()
    assert p.status == PositionStatus.OPEN


def test_position_settled_status():
    p = _make_position(
        status=PositionStatus.SETTLED,
        ts_settled=datetime.now(timezone.utc),
        pnl=0.30,
    )
    assert p.status == PositionStatus.SETTLED
    assert p.pnl == pytest.approx(0.30)
    assert p.ts_settled is not None


def test_position_price_bounds():
    with pytest.raises(ValidationError):
        _make_position(poly_price=1.01)
    with pytest.raises(ValidationError):
        _make_position(kalshi_price=-0.01)


def test_trade_size_must_be_positive():
    with pytest.raises(ValidationError):
        _make_position(trade_size_usdc=0.0)


def test_position_is_frozen():
    p = _make_position()
    with pytest.raises(Exception):
        p.status = PositionStatus.SETTLED  # type: ignore[misc]


def test_position_model_copy_for_settlement():
    p = _make_position()
    settled = p.model_copy(
        update={
            "status": PositionStatus.SETTLED,
            "ts_settled": datetime.now(timezone.utc),
            "pnl": 0.25,
        }
    )
    assert settled.status == PositionStatus.SETTLED
    assert settled.pnl == pytest.approx(0.25)
    assert p.status == PositionStatus.OPEN  # original unchanged


def test_live_mode():
    p = _make_position(mode=TradingMode.LIVE)
    assert p.mode == TradingMode.LIVE


def test_direction_b_position():
    p = _make_position(
        direction=Direction.B,
        poly_side=Side.NO,
        kalshi_side=Side.YES,
        poly_price=0.46,
        kalshi_price=0.54,
    )
    assert p.direction == Direction.B
    assert p.poly_side == Side.NO
    assert p.kalshi_side == Side.YES
