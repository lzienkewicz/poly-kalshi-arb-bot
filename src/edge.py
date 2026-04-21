"""
Edge calculator: evaluates a confirmed EventPair against live price snapshots.

Pipeline (per PRODUCT_SPEC.md §6-8)
-------------------------------------
1. Compute gross_edge for Direction A and Direction B.
2. Pick the direction with the higher gross_edge.
3. Apply stale-data penalty (per-leg).
4. Classify the result:
     TRADEABLE        net_edge >= NET_EDGE_MIN AND depth check passes
     REJECTED_STALE   gross_edge >= NET_EDGE_MIN but net_edge (after penalty) < NET_EDGE_MIN
     REJECTED_DEPTH   net_edge >= NET_EDGE_MIN but insufficient book depth
     REJECTED_EDGE    net_edge < NET_EDGE_MIN (not stale-caused)

Inputs
------
- poly_snap   : CanonicalSnapshot for the Polymarket leg
- kalshi_snap : CanonicalSnapshot for the Kalshi leg
- pair         : EventPair (must be is_tradeable — caller's responsibility)
- cfg          : EdgeConfig (fees, slippage, thresholds)
- now          : evaluation timestamp (injectable for testing)

Output
------
Opportunity dataclass — always returned regardless of status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

import structlog

from src.models.event_pair import EventPair
from src.models.opportunity import Opportunity, OpportunityStatus, TradeDirection
from src.models.orderbook import OrderBook
from src.normalization import CanonicalSnapshot

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeConfig:
    """All tunable parameters for the edge calculator.

    Defaults match PRODUCT_SPEC.md §6 / §10.
    """
    poly_fee: float = 0.0          # taker fee rate (fraction, not %)
    kalshi_fee: float = 0.02       # 2% of notional
    slippage_per_leg: float = 0.005
    stale_penalty_per_leg: float = 0.005
    max_price_age_s: float = 30.0
    net_edge_min: float = 0.02
    trade_size_usdc: float = 10.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _LegCost(NamedTuple):
    ask: float          # best ask price in [0, 1]
    available: float    # contracts available at best ask
    cost_with_fee: float  # ask * (1 + fee)


def _leg_cost(ask: float, fee: float, slippage: float) -> float:
    """Total cost of one leg: ask * (1 + fee) + slippage."""
    return ask * (1.0 + fee) + slippage


def _is_stale(snap: CanonicalSnapshot, now: datetime, max_age_s: float) -> bool:
    age = (now - snap.snapshot_ts).total_seconds()
    return age > max_age_s


def _depth_ok(ask: float, available: float, trade_size_usdc: float) -> bool:
    """True if there are enough contracts at the ask to fill trade_size_usdc."""
    if ask <= 0:
        return False
    contracts_needed = trade_size_usdc / ask
    return available >= contracts_needed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_edge(
    poly_snap: CanonicalSnapshot,
    kalshi_snap: CanonicalSnapshot,
    pair: EventPair,
    poly_yes_book: OrderBook,
    kalshi_no_book: OrderBook,
    poly_no_book: OrderBook,
    kalshi_yes_book: OrderBook,
    cfg: EdgeConfig,
    now: datetime | None = None,
) -> Opportunity:
    """Evaluate a confirmed EventPair and return a classified Opportunity.

    Parameters
    ----------
    poly_snap / kalshi_snap:
        Price snapshots for the Polymarket and Kalshi legs.
    pair:
        The confirmed EventPair. Must be is_tradeable (caller guarantees this).
    poly_yes_book / kalshi_no_book / poly_no_book / kalshi_yes_book:
        Raw OrderBook objects stored on the Opportunity for audit/logging.
        The edge calculation uses data from the CanonicalSnapshots, not these directly.
    cfg:
        Fee, slippage, and threshold configuration.
    now:
        Evaluation timestamp; defaults to UTC now. Injectable for testing.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # ── Stale detection ───────────────────────────────────────────────────
    poly_stale = _is_stale(poly_snap, now, cfg.max_price_age_s)
    kalshi_stale = _is_stale(kalshi_snap, now, cfg.max_price_age_s)
    stale_legs = int(poly_stale) + int(kalshi_stale)
    stale_penalty = stale_legs * cfg.stale_penalty_per_leg

    # ── Direction A: Buy YES on Poly, Buy NO on Kalshi ────────────────────
    gross_edge_a: float | None = None
    if poly_snap.yes_ask is not None and kalshi_snap.no_ask is not None:
        cost_a = (
            _leg_cost(poly_snap.yes_ask, cfg.poly_fee, cfg.slippage_per_leg)
            + _leg_cost(kalshi_snap.no_ask, cfg.kalshi_fee, cfg.slippage_per_leg)
        )
        gross_edge_a = 1.0 - cost_a

    # ── Direction B: Buy NO on Poly, Buy YES on Kalshi ────────────────────
    gross_edge_b: float | None = None
    if poly_snap.no_ask is not None and kalshi_snap.yes_ask is not None:
        cost_b = (
            _leg_cost(poly_snap.no_ask, cfg.poly_fee, cfg.slippage_per_leg)
            + _leg_cost(kalshi_snap.yes_ask, cfg.kalshi_fee, cfg.slippage_per_leg)
        )
        gross_edge_b = 1.0 - cost_b

    # ── Pick best direction ───────────────────────────────────────────────
    if gross_edge_a is None and gross_edge_b is None:
        return _reject(
            pair=pair,
            poly_yes_book=poly_yes_book,
            kalshi_no_book=kalshi_no_book,
            now=now,
            direction=TradeDirection.A,
            poly_ask=0.0,
            kalshi_ask=0.0,
            gross_edge=0.0,
            fee_estimate=0.0,
            slippage_buffer=2 * cfg.slippage_per_leg,
            stale_penalty=stale_penalty,
            poly_stale=poly_stale,
            kalshi_stale=kalshi_stale,
            trade_size=cfg.trade_size_usdc,
            status=OpportunityStatus.REJECTED_EDGE,
            reason="NO_ASKS",
            detail="One or both snapshots missing ask prices",
        )

    if gross_edge_a is None:
        direction = TradeDirection.B
        gross_edge = gross_edge_b  # type: ignore[assignment]
        poly_ask = poly_snap.no_ask  # type: ignore[assignment]
        kalshi_ask = kalshi_snap.yes_ask  # type: ignore[assignment]
        poly_available = poly_snap.no_available_at_ask
        kalshi_available = kalshi_snap.yes_available_at_ask
    elif gross_edge_b is None or gross_edge_a >= gross_edge_b:
        direction = TradeDirection.A
        gross_edge = gross_edge_a
        poly_ask = poly_snap.yes_ask  # type: ignore[assignment]
        kalshi_ask = kalshi_snap.no_ask  # type: ignore[assignment]
        poly_available = poly_snap.yes_available_at_ask
        kalshi_available = kalshi_snap.no_available_at_ask
    else:
        direction = TradeDirection.B
        gross_edge = gross_edge_b
        poly_ask = poly_snap.no_ask  # type: ignore[assignment]
        kalshi_ask = kalshi_snap.yes_ask  # type: ignore[assignment]
        poly_available = poly_snap.no_available_at_ask
        kalshi_available = kalshi_snap.yes_available_at_ask

    # ── Fee + slippage components (for Opportunity fields) ────────────────
    fee_estimate = poly_ask * cfg.poly_fee + kalshi_ask * cfg.kalshi_fee
    slippage_buffer = 2 * cfg.slippage_per_leg
    net_edge = gross_edge - fee_estimate - slippage_buffer - stale_penalty

    # ── Pick the appropriate raw books for the chosen direction ───────────
    chosen_poly_book = poly_yes_book if direction == TradeDirection.A else poly_no_book
    chosen_kalshi_book = kalshi_no_book if direction == TradeDirection.A else kalshi_yes_book

    log.debug(
        "edge_calculated",
        pair_key=pair.pair_key,
        direction=direction,
        gross_edge=round(gross_edge, 6),
        net_edge=round(net_edge, 6),
        poly_stale=poly_stale,
        kalshi_stale=kalshi_stale,
    )

    # ── Stale rejection (edge existed before penalty, gone after) ─────────
    pre_stale_net = gross_edge - fee_estimate - slippage_buffer
    if pre_stale_net >= cfg.net_edge_min > net_edge:
        return _reject(
            pair=pair,
            poly_yes_book=chosen_poly_book,
            kalshi_no_book=chosen_kalshi_book,
            now=now,
            direction=direction,
            poly_ask=poly_ask,
            kalshi_ask=kalshi_ask,
            gross_edge=gross_edge,
            fee_estimate=fee_estimate,
            slippage_buffer=slippage_buffer,
            stale_penalty=stale_penalty,
            poly_stale=poly_stale,
            kalshi_stale=kalshi_stale,
            trade_size=cfg.trade_size_usdc,
            status=OpportunityStatus.REJECTED_STALE,
            reason="STALE_KILLED_EDGE",
            detail=f"gross_edge={gross_edge:.4f} pre-penalty net={pre_stale_net:.4f} post-penalty net={net_edge:.4f}",
        )

    # ── Edge floor ────────────────────────────────────────────────────────
    if net_edge < cfg.net_edge_min:
        return _reject(
            pair=pair,
            poly_yes_book=chosen_poly_book,
            kalshi_no_book=chosen_kalshi_book,
            now=now,
            direction=direction,
            poly_ask=poly_ask,
            kalshi_ask=kalshi_ask,
            gross_edge=gross_edge,
            fee_estimate=fee_estimate,
            slippage_buffer=slippage_buffer,
            stale_penalty=stale_penalty,
            poly_stale=poly_stale,
            kalshi_stale=kalshi_stale,
            trade_size=cfg.trade_size_usdc,
            status=OpportunityStatus.REJECTED_EDGE,
            reason="BELOW_MIN_EDGE",
            detail=f"net_edge={net_edge:.4f} < min={cfg.net_edge_min}",
        )

    # ── Depth check ───────────────────────────────────────────────────────
    if not _depth_ok(poly_ask, poly_available, cfg.trade_size_usdc):
        return _reject(
            pair=pair,
            poly_yes_book=chosen_poly_book,
            kalshi_no_book=chosen_kalshi_book,
            now=now,
            direction=direction,
            poly_ask=poly_ask,
            kalshi_ask=kalshi_ask,
            gross_edge=gross_edge,
            fee_estimate=fee_estimate,
            slippage_buffer=slippage_buffer,
            stale_penalty=stale_penalty,
            poly_stale=poly_stale,
            kalshi_stale=kalshi_stale,
            trade_size=cfg.trade_size_usdc,
            status=OpportunityStatus.REJECTED_DEPTH,
            reason="POLY_DEPTH_INSUFFICIENT",
            detail=f"available={poly_available:.1f} needed={cfg.trade_size_usdc / poly_ask:.1f}",
        )

    if not _depth_ok(kalshi_ask, kalshi_available, cfg.trade_size_usdc):
        return _reject(
            pair=pair,
            poly_yes_book=chosen_poly_book,
            kalshi_no_book=chosen_kalshi_book,
            now=now,
            direction=direction,
            poly_ask=poly_ask,
            kalshi_ask=kalshi_ask,
            gross_edge=gross_edge,
            fee_estimate=fee_estimate,
            slippage_buffer=slippage_buffer,
            stale_penalty=stale_penalty,
            poly_stale=poly_stale,
            kalshi_stale=kalshi_stale,
            trade_size=cfg.trade_size_usdc,
            status=OpportunityStatus.REJECTED_DEPTH,
            reason="KALSHI_DEPTH_INSUFFICIENT",
            detail=f"available={kalshi_available:.1f} needed={cfg.trade_size_usdc / kalshi_ask:.1f}",
        )

    # ── TRADEABLE ─────────────────────────────────────────────────────────
    log.info(
        "edge_tradeable",
        pair_key=pair.pair_key,
        direction=direction,
        net_edge=round(net_edge, 6),
        estimated_profit=round(net_edge * cfg.trade_size_usdc, 4),
    )
    return Opportunity(
        pair=pair,
        poly_book=chosen_poly_book,
        kalshi_book=chosen_kalshi_book,
        evaluated_at=now,
        direction=direction,
        poly_ask=poly_ask,
        kalshi_ask=kalshi_ask,
        gross_edge=gross_edge,
        fee_estimate=fee_estimate,
        slippage_buffer=slippage_buffer,
        stale_penalty=stale_penalty,
        net_edge=net_edge,
        poly_book_stale=poly_stale,
        kalshi_book_stale=kalshi_stale,
        executable_size_usdc=cfg.trade_size_usdc,
        status=OpportunityStatus.TRADEABLE,
    )


# ---------------------------------------------------------------------------
# Private builder for rejection paths
# ---------------------------------------------------------------------------

def _reject(
    *,
    pair: EventPair,
    poly_yes_book: OrderBook,
    kalshi_no_book: OrderBook,
    now: datetime,
    direction: TradeDirection,
    poly_ask: float,
    kalshi_ask: float,
    gross_edge: float,
    fee_estimate: float,
    slippage_buffer: float,
    stale_penalty: float,
    poly_stale: bool,
    kalshi_stale: bool,
    trade_size: float,
    status: OpportunityStatus,
    reason: str,
    detail: str,
) -> Opportunity:
    net_edge = gross_edge - fee_estimate - slippage_buffer - stale_penalty
    return Opportunity(
        pair=pair,
        poly_book=poly_yes_book,
        kalshi_book=kalshi_no_book,
        evaluated_at=now,
        direction=direction,
        poly_ask=max(poly_ask, 0.0),
        kalshi_ask=max(kalshi_ask, 0.0),
        gross_edge=gross_edge,
        fee_estimate=fee_estimate,
        slippage_buffer=slippage_buffer,
        stale_penalty=stale_penalty,
        net_edge=net_edge,
        poly_book_stale=poly_stale,
        kalshi_book_stale=kalshi_stale,
        executable_size_usdc=trade_size,
        status=status,
        fail_reason=reason,
        fail_detail=detail,
    )
