"""
Paper trading simulator for approved exact arbitrage pairs.

Tracks simulated fills, hypothetical PnL, and enforces risk controls
without placing any real trades.  State persists across sessions as JSONL
(one JSON record per line) so the portfolio survives restarts.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.models.opportunity import Opportunity, OpportunityStatus

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskLimits:
    """Hard limits applied before every simulated fill."""
    max_position_per_market_usdc: float = 50.0
    max_total_exposure_usdc: float = 200.0
    min_liquidity_per_leg_usdc: float = 5.0
    cooldown_s: float = 300.0          # seconds between re-entering the same pair


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class PaperTrade:
    """One simulated two-legged fill persisted to disk."""
    trade_id: str
    timestamp: str                    # ISO-8601 UTC
    pair_key: str
    poly_market_id: str
    kalshi_market_id: str
    direction: str                    # "A" or "B"
    poly_side: str                    # "YES" or "NO"
    kalshi_side: str                  # "YES" or "NO"
    poly_fill_price: float
    kalshi_fill_price: float
    contracts: float
    notional_usdc: float
    gross_edge: float
    fee_estimate: float
    slippage_buffer: float
    net_edge: float
    estimated_profit_usdc: float
    status: str = "open"              # "open" | "settled"


# ---------------------------------------------------------------------------
# Paper trader
# ---------------------------------------------------------------------------

class PaperTrader:
    """
    Simulates arbitrage execution against live prices.

    All fills are synthetic — no real orders are placed.  State persists to
    a JSONL file so the portfolio survives restarts.

    Usage::

        trader = PaperTrader(Path("data/paper_trades.jsonl"))
        trade = trader.execute(opportunity)   # None if risk controls reject it
        print(trader.portfolio_summary())
    """

    def __init__(self, db_path: Path, limits: RiskLimits | None = None) -> None:
        self._db_path = db_path
        self._limits = limits or RiskLimits()
        self._trades: list[PaperTrade] = self._load()
        # Most-recent fill timestamp per pair_key (for cooldown enforcement)
        self._last_fill_ts: dict[str, datetime] = {}
        for t in self._trades:
            ts = datetime.fromisoformat(t.timestamp)
            pk = t.pair_key
            if pk not in self._last_fill_ts or ts > self._last_fill_ts[pk]:
                self._last_fill_ts[pk] = ts

    # ------------------------------------------------------------------
    # Risk gate
    # ------------------------------------------------------------------

    def can_enter(self, opp: Opportunity) -> tuple[bool, str]:
        """Return (allowed, rejection_reason).  Empty reason string means allowed."""
        if opp.status != OpportunityStatus.TRADEABLE:
            return False, f"not_tradeable:{opp.status}"

        pk = opp.pair.pair_key
        lim = self._limits

        # 1. Cooldown per pair
        last = self._last_fill_ts.get(pk)
        if last is not None:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            remaining = lim.cooldown_s - elapsed
            if remaining > 0:
                return False, f"cooldown:{int(remaining)}s_remaining"

        # 2. No duplicate open position in same market
        open_keys = {t.pair_key for t in self._trades if t.status == "open"}
        if pk in open_keys:
            return False, "duplicate_open_position"

        # 3. Per-market exposure cap
        market_deployed = sum(
            t.notional_usdc for t in self._trades
            if t.pair_key == pk and t.status == "open"
        )
        if market_deployed + opp.executable_size_usdc > lim.max_position_per_market_usdc:
            return False, (
                f"market_cap_exceeded:{market_deployed:.2f}"
                f"+{opp.executable_size_usdc:.2f}>{lim.max_position_per_market_usdc:.2f}"
            )

        # 4. Total portfolio exposure cap
        total_deployed = sum(t.notional_usdc for t in self._trades if t.status == "open")
        if total_deployed + opp.executable_size_usdc > lim.max_total_exposure_usdc:
            return False, (
                f"portfolio_cap_exceeded:{total_deployed:.2f}"
                f"+{opp.executable_size_usdc:.2f}>{lim.max_total_exposure_usdc:.2f}"
            )

        # 5. Minimum liquidity per leg (USD notional at best ask)
        poly_liq = opp.poly_book.asks.total_available * opp.poly_ask
        kalshi_liq = opp.kalshi_book.asks.total_available * opp.kalshi_ask
        if poly_liq < lim.min_liquidity_per_leg_usdc:
            return False, (
                f"poly_insufficient_liquidity:{poly_liq:.2f}"
                f"<{lim.min_liquidity_per_leg_usdc:.2f}"
            )
        if kalshi_liq < lim.min_liquidity_per_leg_usdc:
            return False, (
                f"kalshi_insufficient_liquidity:{kalshi_liq:.2f}"
                f"<{lim.min_liquidity_per_leg_usdc:.2f}"
            )

        return True, ""

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, opp: Opportunity) -> PaperTrade | None:
        """Simulate a fill.  Returns the PaperTrade if accepted, None if rejected."""
        allowed, reason = self.can_enter(opp)
        if not allowed:
            log.info("paper_trade_skipped", pair_key=opp.pair.pair_key, reason=reason)
            return None

        now = datetime.now(timezone.utc)
        contracts = opp.executable_size_usdc / opp.poly_ask if opp.poly_ask > 0 else 0.0

        trade = PaperTrade(
            trade_id=uuid.uuid4().hex[:8],
            timestamp=now.isoformat(),
            pair_key=opp.pair.pair_key,
            poly_market_id=opp.pair.polymarket.venue_market_id,
            kalshi_market_id=opp.pair.kalshi.venue_market_id,
            direction=str(opp.direction),
            poly_side=opp.poly_side,
            kalshi_side=opp.kalshi_side,
            poly_fill_price=round(opp.poly_ask, 6),
            kalshi_fill_price=round(opp.kalshi_ask, 6),
            contracts=round(contracts, 4),
            notional_usdc=opp.executable_size_usdc,
            gross_edge=round(opp.gross_edge, 6),
            fee_estimate=round(opp.fee_estimate, 6),
            slippage_buffer=round(opp.slippage_buffer, 6),
            net_edge=round(opp.net_edge, 6),
            estimated_profit_usdc=round(opp.estimated_profit_usdc, 4),
        )

        self._trades.append(trade)
        self._last_fill_ts[trade.pair_key] = now
        self._persist(trade)

        log.info(
            "paper_trade_executed",
            trade_id=trade.trade_id,
            pair_key=trade.pair_key,
            direction=trade.direction,
            poly_fill=trade.poly_fill_price,
            kalshi_fill=trade.kalshi_fill_price,
            net_edge=trade.net_edge,
            profit_usdc=trade.estimated_profit_usdc,
        )
        return trade

    # ------------------------------------------------------------------
    # Portfolio state
    # ------------------------------------------------------------------

    def open_trades(self) -> list[PaperTrade]:
        return [t for t in self._trades if t.status == "open"]

    def all_trades(self) -> list[PaperTrade]:
        return list(self._trades)

    def portfolio_summary(self) -> dict:
        open_t = self.open_trades()
        return {
            "total_trades_all_time": len(self._trades),
            "open_positions": len(open_t),
            "total_deployed_usdc": round(sum(t.notional_usdc for t in open_t), 2),
            "hypothetical_pnl_usdc": round(
                sum(t.estimated_profit_usdc for t in self._trades), 4
            ),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> list[PaperTrade]:
        if not self._db_path.exists():
            return []
        trades: list[PaperTrade] = []
        for line in self._db_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(PaperTrade(**json.loads(line)))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                log.warning("paper_trade_load_error", line=line[:80], error=str(exc))
        return trades

    def _persist(self, trade: PaperTrade) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
