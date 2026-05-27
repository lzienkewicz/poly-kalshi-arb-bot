"""
Pricing diagnostics and paper-trading edge analysis for approved exact politics pairs.

Loads every pair from config/approved_pairs.json, fetches live quotes from both
Polymarket and Kalshi, evaluates arbitrage edge using the same calculation as the
live bot, and runs paper-trading simulations for any tradeable opportunity found.

Usage:
    python pricing_diagnostics.py

Paper mode only — no live trades are ever placed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.edge import EdgeConfig, calculate_edge
from src.models.event_pair import EventPair, MatchConfidence, MatchStatus
from src.models.market import Market
from src.models.opportunity import Opportunity, OpportunityStatus
from src.models.orderbook import OrderBook
from src.normalization import normalize_snapshot
from src.paper_trader import PaperTrader, RiskLimits
from src.venues.kalshi.adapter import KalshiAdapter
from src.venues.kalshi.client import KalshiClient
from src.venues.polymarket.adapter import PolymarketAdapter
from src.venues.polymarket.client import PolymarketClient

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_APPROVED_PAIRS_PATH = Path("config/approved_pairs.json")
_PAPER_TRADES_PATH = Path("data/paper_trades.jsonl")
_QUOTE_TTL_S = 30.0
_KALSHI_INTER_REQUEST_DELAY_S = 0.4   # seconds between Kalshi fetches (rate-limit guard)
_TOP_N = 20                            # opportunities shown in summary

_EDGE_CFG = EdgeConfig(
    poly_fee=0.0,
    kalshi_fee=0.02,
    slippage_per_leg=0.005,
    stale_penalty_per_leg=0.005,
    max_price_age_s=30.0,
    net_edge_min=0.02,
    trade_size_usdc=10.0,
)

_RISK = RiskLimits(
    max_position_per_market_usdc=50.0,
    max_total_exposure_usdc=200.0,
    min_liquidity_per_leg_usdc=5.0,
    cooldown_s=300.0,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ApprovedPair:
    poly_id: str
    kalshi_id: str
    note: str
    directions_inverted: bool = False


@dataclass
class PricedResult:
    approved: ApprovedPair
    poly_market: Market | None
    kalshi_market: Market | None
    poly_yes_book: OrderBook | None
    poly_no_book: OrderBook | None
    kalshi_yes_book: OrderBook | None
    kalshi_no_book: OrderBook | None
    opportunity: Opportunity | None
    error: str | None = None
    fallback_price_used: bool = False  # True when Gamma prices substitute for CLOB books


# ---------------------------------------------------------------------------
# Loading approved pairs
# ---------------------------------------------------------------------------

def load_approved_pairs(path: Path = _APPROVED_PAIRS_PATH) -> list[ApprovedPair]:
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        ApprovedPair(
            poly_id=p["polymarket_id"],
            kalshi_id=p["kalshi_id"],
            note=p.get("note", ""),
            directions_inverted=bool(p.get("directions_inverted", False)),
        )
        for p in data.get("pairs", [])
        if "polymarket_id" in p and "kalshi_id" in p
    ]


# ---------------------------------------------------------------------------
# Token-ID extraction — handles both Gamma payload formats
# ---------------------------------------------------------------------------

def _poly_token_ids(market: Market) -> tuple[str | None, str | None]:
    """Return (yes_token_id, no_token_id) from a Polymarket market's raw payload.

    Handles two Gamma formats:
      Format A – tokens array: [{"token_id": "...", "outcome": "Yes"}, ...]
      Format B – clobTokenIds list + outcomes JSON string
    """
    import json as _json

    raw = market.raw
    yes_id: str | None = None
    no_id: str | None = None

    # Format A: tokens array
    for token in raw.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome", "")).lower()
        tid = token.get("token_id")
        if tid and outcome == "yes":
            yes_id = str(tid)
        elif tid and outcome == "no":
            no_id = str(tid)
    if yes_id or no_id:
        return yes_id, no_id

    # Format B: clobTokenIds + outcomes JSON string
    clob_ids = raw.get("clobTokenIds") or []
    outcomes_raw = raw.get("outcomes")
    if clob_ids and outcomes_raw:
        try:
            outcomes = (
                _json.loads(outcomes_raw)
                if isinstance(outcomes_raw, str)
                else outcomes_raw
            )
            for i, outcome in enumerate(outcomes or []):
                if i >= len(clob_ids):
                    break
                if str(outcome).lower() == "yes":
                    yes_id = str(clob_ids[i])
                elif str(outcome).lower() == "no":
                    no_id = str(clob_ids[i])
        except (_json.JSONDecodeError, TypeError):
            pass

    return yes_id, no_id


# ---------------------------------------------------------------------------
# Kalshi quote cache (TTL-based, reduces rate-limit pressure)
# ---------------------------------------------------------------------------

class _KalshiCache:
    def __init__(self, ttl_s: float = _QUOTE_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._store: dict[str, tuple[datetime, Market, OrderBook, OrderBook]] = {}

    def get(self, ticker: str) -> tuple[Market, OrderBook, OrderBook] | None:
        entry = self._store.get(ticker)
        if entry is None:
            return None
        ts, market, yes_book, no_book = entry
        if (datetime.now(timezone.utc) - ts).total_seconds() > self._ttl_s:
            return None
        return market, yes_book, no_book

    def put(
        self,
        ticker: str,
        market: Market,
        yes_book: OrderBook,
        no_book: OrderBook,
    ) -> None:
        self._store[ticker] = (datetime.now(timezone.utc), market, yes_book, no_book)


# ---------------------------------------------------------------------------
# Polymarket diagnostic probe (run once, before the pricing loop)
# ---------------------------------------------------------------------------

async def _probe_poly_pair(adapter: PolymarketAdapter, ap: ApprovedPair) -> None:
    """Print resolution details for one approved pair as a sanity check."""
    print()
    print("  ── Polymarket resolution probe ──────────────────────────────")
    print(f"  condition_id : {ap.poly_id}")
    market = await adapter.resolve_market_by_condition_id(ap.poly_id)
    if market is None:
        print("  result       : NOT FOUND (check condition_id in approved_pairs.json)")
        print("  ─────────────────────────────────────────────────────────────")
        return
    raw = market.raw
    yes_id, no_id = _poly_token_ids(market)
    print(f"  question     : {market.question}")
    print(f"  is_open      : {market.is_open}")
    print(f"  yes_token_id : {yes_id}")
    print(f"  no_token_id  : {no_id}")
    # Show all outcome tokens if present
    for tok in raw.get("tokens") or []:
        if isinstance(tok, dict):
            print(f"  token        : {tok.get('outcome')} → {tok.get('token_id')}")
    # Gamma price fields
    for field in ("bestAsk", "bestBid", "lastTradePrice", "best_ask", "best_bid"):
        val = raw.get(field)
        if val is not None:
            print(f"  {field:<16} : {val}")
    print("  ─────────────────────────────────────────────────────────────")
    print()


# ---------------------------------------------------------------------------
# Async fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_poly(
    adapter: PolymarketAdapter,
    poly_id: str,
) -> tuple[Market, OrderBook, OrderBook, bool]:
    """Fetch Polymarket market + orderbooks for a condition_id.

    Returns (market, yes_book, no_book, fallback_used).

    Resolution uses the correct query-parameter lookup (?condition_id=0x...) so
    the old path-based call that returns HTTP 422 is never made.

    If the CLOB orderbook fetch fails, falls back to Gamma price fields and sets
    fallback_used=True.  The synthetic books have size=0, so depth checks in the
    edge calculator will classify the result as INSUFFICIENT_LIQUIDITY rather
    than TRADEABLE — caller marks status accordingly.
    """
    # Step 1: resolve market by condition_id (query param, not path)
    market = await adapter.resolve_market_by_condition_id(poly_id)
    if market is None:
        raise RuntimeError(f"poly_condition_id_not_found:{poly_id[:20]}")

    # Step 2: extract CLOB token IDs
    yes_token_id, no_token_id = _poly_token_ids(market)
    if not yes_token_id or not no_token_id:
        log.warning("poly_tokens_missing", condition_id=poly_id[:20])
        raise RuntimeError(f"poly_tokens_missing:{poly_id[:20]}")

    # Step 3: attempt CLOB orderbook fetch
    try:
        yes_book, no_book = await adapter.fetch_orderbooks(
            poly_id, yes_token_id, no_token_id
        )
        return market, yes_book, no_book, False
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "poly_clob_fetch_failed_using_gamma_fallback",
            condition_id=poly_id[:20],
            error=str(exc)[:120],
        )
        # Step 4: fallback — build synthetic books from Gamma price fields
        yes_book, no_book = PolymarketAdapter.build_orderbooks_from_gamma_prices(
            poly_id, market.raw
        )
        return market, yes_book, no_book, True


async def _fetch_kalshi(
    adapter: KalshiAdapter,
    ticker: str,
    cache: _KalshiCache,
    rate_lock: asyncio.Lock,
    last_request_time: list[float],
) -> tuple[Market, OrderBook, OrderBook] | None:
    cached = cache.get(ticker)
    if cached is not None:
        return cached
    try:
        # Pace consecutive Kalshi requests to avoid 429s
        async with rate_lock:
            now = asyncio.get_event_loop().time()
            wait = _KALSHI_INTER_REQUEST_DELAY_S - (now - last_request_time[0])
            if wait > 0:
                await asyncio.sleep(wait)
            last_request_time[0] = asyncio.get_event_loop().time()

        market = await adapter.fetch_market(ticker)
        yes_book, no_book = await adapter.fetch_orderbooks(ticker)
        cache.put(ticker, market, yes_book, no_book)
        return market, yes_book, no_book
    except Exception as exc:  # noqa: BLE001
        log.warning("kalshi_fetch_error", ticker=ticker, error=str(exc)[:120])
        return None


# ---------------------------------------------------------------------------
# EventPair construction for an approved pair
# ---------------------------------------------------------------------------

def _make_pair(poly: Market, kalshi: Market, approved: ApprovedPair) -> EventPair:
    return EventPair(
        polymarket=poly,
        kalshi=kalshi,
        status=MatchStatus.MATCHED,
        confidence=MatchConfidence.EXACT,
        directions_inverted=approved.directions_inverted,
    )


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _disp_status(opp: Opportunity) -> str:
    if opp.status == OpportunityStatus.TRADEABLE:
        return "TRADEABLE"
    if opp.status == OpportunityStatus.REJECTED_STALE:
        return "STALE_QUOTES"
    if opp.status == OpportunityStatus.REJECTED_DEPTH:
        return "INSUFFICIENT_LIQUIDITY"
    return "NO_EDGE"


def _fp(val: float | None, width: int = 6) -> str:
    return f"{val:.4f}" if val is not None else " N/A  "


def _depth_usd(book: OrderBook | None) -> str:
    if book is None:
        return "   N/A"
    usd = book.asks.total_available * (book.best_ask or 0.0)
    return f"${usd:6.1f}"


def _book_line(label: str, book: OrderBook | None) -> str:
    if book is None:
        return f"  {label:<8}  bid=  N/A  ask=  N/A  mid=  N/A  spr=  N/A  depth=   N/A"
    bid = _fp(book.best_bid)
    ask = _fp(book.best_ask)
    mid = _fp(book.mid)
    spr = f"{book.spread:.4f}" if book.spread is not None else " N/A "
    depth = _depth_usd(book)
    return f"  {label:<8}  bid={bid}  ask={ask}  mid={mid}  spr={spr}  depth={depth}"


# ---------------------------------------------------------------------------
# Pricing loop
# ---------------------------------------------------------------------------

async def price_all_pairs(
    pairs: list[ApprovedPair],
    poly_adapter: PolymarketAdapter,
    kalshi_adapter: KalshiAdapter,
) -> list[PricedResult]:
    cache = _KalshiCache()
    rate_lock = asyncio.Lock()
    last_request_time: list[float] = [0.0]
    results: list[PricedResult] = []

    for i, ap in enumerate(pairs, 1):
        print(f"  [{i:>2}/{len(pairs)}] {ap.kalshi_id:<30}", end=" ", flush=True)

        # ── Polymarket ────────────────────────────────────────────────────────
        poly_market: Market | None = None
        poly_yes_book: OrderBook | None = None
        poly_no_book: OrderBook | None = None
        poly_fallback = False
        poly_err: str | None = None

        try:
            poly_market, poly_yes_book, poly_no_book, poly_fallback = await _fetch_poly(
                poly_adapter, ap.poly_id
            )
        except RuntimeError as exc:
            poly_err = str(exc)
        except Exception as exc:  # noqa: BLE001
            poly_err = f"poly_fetch_error:{str(exc)[:80]}"

        if poly_err:
            print(f"POLY ERROR ({poly_err})")
            results.append(PricedResult(
                approved=ap,
                poly_market=None, kalshi_market=None,
                poly_yes_book=None, poly_no_book=None,
                kalshi_yes_book=None, kalshi_no_book=None,
                opportunity=None, error=poly_err,
            ))
            continue

        # ── Kalshi ────────────────────────────────────────────────────────────
        kalshi_result = await _fetch_kalshi(
            kalshi_adapter, ap.kalshi_id, cache, rate_lock, last_request_time
        )
        if kalshi_result is None:
            print("KALSHI ERROR (fetch failed)")
            results.append(PricedResult(
                approved=ap,
                poly_market=poly_market, kalshi_market=None,
                poly_yes_book=poly_yes_book, poly_no_book=poly_no_book,
                kalshi_yes_book=None, kalshi_no_book=None,
                opportunity=None, error="kalshi_fetch_failed",
            ))
            continue

        kalshi_market, kalshi_yes_book, kalshi_no_book = kalshi_result

        # ── Market-closed guard ───────────────────────────────────────────────
        assert poly_market is not None
        if not poly_market.is_open:
            print("CLOSED (polymarket)")
            results.append(PricedResult(
                approved=ap,
                poly_market=poly_market, kalshi_market=kalshi_market,
                poly_yes_book=poly_yes_book, poly_no_book=poly_no_book,
                kalshi_yes_book=kalshi_yes_book, kalshi_no_book=kalshi_no_book,
                opportunity=None, error="poly_market_closed",
            ))
            continue

        if not kalshi_market.is_open:
            print("CLOSED (kalshi)")
            results.append(PricedResult(
                approved=ap,
                poly_market=poly_market, kalshi_market=kalshi_market,
                poly_yes_book=poly_yes_book, poly_no_book=poly_no_book,
                kalshi_yes_book=kalshi_yes_book, kalshi_no_book=kalshi_no_book,
                opportunity=None, error="kalshi_market_closed",
            ))
            continue

        # ── Edge calculation ──────────────────────────────────────────────────
        assert poly_yes_book is not None and poly_no_book is not None
        pair = _make_pair(poly_market, kalshi_market, ap)
        poly_snap = normalize_snapshot(poly_yes_book, poly_no_book)
        kalshi_snap = normalize_snapshot(kalshi_yes_book, kalshi_no_book)

        if ap.directions_inverted:
            log.warning("directions_inverted_pair", pair_key=pair.pair_key)

        opp = calculate_edge(
            poly_snap=poly_snap,
            kalshi_snap=kalshi_snap,
            pair=pair,
            poly_yes_book=poly_yes_book,
            kalshi_no_book=kalshi_no_book,
            poly_no_book=poly_no_book,
            kalshi_yes_book=kalshi_yes_book,
            cfg=_EDGE_CFG,
        )

        status_str = _disp_status(opp)
        fallback_tag = "  [GAMMA_FALLBACK]" if poly_fallback else ""
        if opp.status == OpportunityStatus.TRADEABLE:
            edge_str = f"  NET={opp.net_edge:+.4f} *** TRADEABLE ***"
        else:
            edge_str = f"  net={opp.net_edge:+.4f}"
        print(status_str + edge_str + fallback_tag)

        err_tag = "fallback_price_used" if poly_fallback else None
        results.append(PricedResult(
            approved=ap,
            poly_market=poly_market,
            kalshi_market=kalshi_market,
            poly_yes_book=poly_yes_book,
            poly_no_book=poly_no_book,
            kalshi_yes_book=kalshi_yes_book,
            kalshi_no_book=kalshi_no_book,
            opportunity=opp,
            error=err_tag,
            fallback_price_used=poly_fallback,
        ))

    return results


# ---------------------------------------------------------------------------
# Per-pair detail output
# ---------------------------------------------------------------------------

def _print_pair_detail(r: PricedResult, idx: int, total: int) -> None:
    ap = r.approved
    print()
    print(f"[{idx}/{total}]  {ap.note or ap.kalshi_id}")
    print(f"  Poly  :  {ap.poly_id}")
    print(f"  Kalshi:  {ap.kalshi_id}")
    if r.fallback_price_used:
        print("  [GAMMA_FALLBACK] Polymarket CLOB unavailable — using Gamma price fields.")

    if r.error and r.opportunity is None:
        print(f"  >> {r.error.upper()}")
        return

    # Order books
    print(_book_line("Poly YES", r.poly_yes_book))
    print(_book_line("Poly NO ", r.poly_no_book))
    print(_book_line("Kal  YES", r.kalshi_yes_book))
    print(_book_line("Kal  NO ", r.kalshi_no_book))

    if r.opportunity is None:
        return

    opp = r.opportunity

    # Implied probabilities (mid-price)
    p_yes_mid = r.poly_yes_book.mid if r.poly_yes_book else None
    k_yes_mid = r.kalshi_yes_book.mid if r.kalshi_yes_book else None
    p_impl = f"{p_yes_mid*100:.1f}%" if p_yes_mid is not None else "N/A"
    k_impl = f"{k_yes_mid*100:.1f}%" if k_yes_mid is not None else "N/A"
    print(f"  Implied prob:  Poly YES={p_impl}   Kalshi YES={k_impl}")

    print(
        f"  Edge:  gross={opp.gross_edge:+.4f}  fees={opp.fee_estimate:.4f}"
        f"  slip={opp.slippage_buffer:.4f}  net={opp.net_edge:+.4f}"
        f"  exec_size=${opp.executable_size_usdc:.2f}  dir={opp.direction}"
    )
    status = _disp_status(opp)
    bar = "─" * 64
    print(f"  {bar}")
    print(f"  STATUS: {status}{' [GAMMA_FALLBACK]' if r.fallback_price_used else ''}")
    print(f"  {bar}")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _print_summary_table(results: list[PricedResult]) -> None:
    W = 104
    print()
    print("═" * W)
    print("SUMMARY TABLE  (sorted by net edge — best first)")
    print("═" * W)
    print(
        f"  {'#':>3}  {'Kalshi ID':<30}  {'Poly YES ask':>12}  {'Kal YES ask':>11}"
        f"  {'Gross':>7}  {'Net':>8}  {'Dir':>3}  Status"
    )
    print("  " + "─" * (W - 2))

    scored: list[tuple[float, PricedResult]] = [
        (r.opportunity.net_edge if r.opportunity else -999.0, r)
        for r in results
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    for rank, (_, r) in enumerate(scored, 1):
        opp = r.opportunity
        ap = r.approved
        if opp is None:
            print(
                f"  {rank:>3}  {ap.kalshi_id:<30}  {'N/A':>12}  {'N/A':>11}"
                f"  {'N/A':>7}  {'N/A':>8}  {'—':>3}  {r.error or 'UNKNOWN'}"
            )
            continue
        py_ask = f"{opp.poly_ask:.4f}"
        ka_ask = f"{opp.kalshi_ask:.4f}"
        extra = "  [FALLBACK]" if r.fallback_price_used else ""
        print(
            f"  {rank:>3}  {ap.kalshi_id:<30}  {py_ask:>12}  {ka_ask:>11}"
            f"  {opp.gross_edge:>+7.4f}  {opp.net_edge:>+8.4f}  {opp.direction!s:>3}  {_disp_status(opp)}{extra}"
        )


# ---------------------------------------------------------------------------
# Diagnostics summary
# ---------------------------------------------------------------------------

def _print_diagnostics_summary(
    results: list[PricedResult],
    paper_trader: PaperTrader,
) -> None:
    total = len(results)
    errors = sum(1 for r in results if r.error and r.opportunity is None)
    priced = total - errors

    status_counts: Counter[str] = Counter()
    net_edges: list[float] = []
    best: Opportunity | None = None

    for r in results:
        if r.opportunity is not None:
            s = _disp_status(r.opportunity)
            status_counts[s] += 1
            net_edges.append(r.opportunity.net_edge)
            if best is None or r.opportunity.net_edge > best.net_edge:
                best = r.opportunity

    print()
    print("═" * 64)
    print("DIAGNOSTICS SUMMARY")
    print("═" * 64)
    fallbacks = sum(1 for r in results if r.fallback_price_used)
    print(f"  Total approved exact pairs:     {total:>4}")
    print(f"  Successfully priced:            {priced:>4}")
    print(f"  Fetch / market errors:          {errors:>4}")
    print(f"  Gamma-price fallback used:      {fallbacks:>4}")
    print()
    for status, count in sorted(status_counts.items()):
        print(f"  {status:<36} {count:>4}")
    if net_edges:
        avg = sum(net_edges) / len(net_edges)
        print(f"\n  Average net edge:               {avg:>+8.4f}")
    if best is not None:
        print(
            f"  Largest net edge:               {best.net_edge:>+8.4f}"
            f"  ({best.pair.kalshi.venue_market_id})"
        )

    print()
    print("  TOP OPPORTUNITIES:")
    scored = sorted(
        [r for r in results if r.opportunity is not None],
        key=lambda r: r.opportunity.net_edge,  # type: ignore[union-attr]
        reverse=True,
    )
    for i, r in enumerate(scored[:_TOP_N], 1):
        opp = r.opportunity
        assert opp is not None
        est = opp.estimated_profit_usdc
        print(
            f"    {i:>2}. {r.approved.kalshi_id:<32}"
            f"  net={opp.net_edge:>+8.4f}  est_profit=${est:>+6.4f}  {_disp_status(opp)}"
        )

    print()
    print("═" * 64)
    print("PAPER PORTFOLIO")
    print("═" * 64)
    summary = paper_trader.portfolio_summary()
    print(f"  All-time trades:                {summary['total_trades_all_time']:>4}")
    print(f"  Open positions:                 {summary['open_positions']:>4}")
    print(f"  Total deployed:                 ${summary['total_deployed_usdc']:>7.2f}")
    print(f"  Hypothetical PnL:               ${summary['hypothetical_pnl_usdc']:>+8.4f}")

    open_trades = paper_trader.open_trades()
    if open_trades:
        print()
        print("  Open positions:")
        for t in open_trades:
            print(
                f"    #{t.trade_id}  {t.pair_key}"
                f"  dir={t.direction}"
                f"  poly_{t.poly_side}@{t.poly_fill_price:.4f}"
                f"  kal_{t.kalshi_side}@{t.kalshi_fill_price:.4f}"
                f"  net={t.net_edge:>+.4f}"
                f"  profit≈${t.estimated_profit_usdc:>+.4f}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    W = 80
    print("═" * W)
    print(f"  PRICING DIAGNOSTICS — PAPER MODE  {now_str}")
    print("═" * W)

    pairs = load_approved_pairs()
    if not pairs:
        print("No approved pairs loaded. Exiting.")
        return

    print(
        f"\n  {len(pairs)} approved exact pair(s) from {_APPROVED_PAIRS_PATH}"
        f"  |  quote TTL={_QUOTE_TTL_S}s  |  Kalshi pacing={_KALSHI_INTER_REQUEST_DELAY_S}s/req"
    )
    print(
        f"  Edge config: poly_fee={_EDGE_CFG.poly_fee*100:.0f}%"
        f"  kalshi_fee={_EDGE_CFG.kalshi_fee*100:.0f}%"
        f"  slip={_EDGE_CFG.slippage_per_leg*100:.1f}%/leg"
        f"  min_edge={_EDGE_CFG.net_edge_min*100:.0f}¢"
        f"  size=${_EDGE_CFG.trade_size_usdc:.0f}"
    )

    kalshi_api_key = os.environ.get("KALSHI_API_KEY")

    async with (
        KalshiClient(api_key=kalshi_api_key) as k_client,
        PolymarketClient() as p_client,
    ):
        k_adapter = KalshiAdapter(k_client)
        p_adapter = PolymarketAdapter(p_client)

        # Diagnostic probe: print resolution details for the first pair only
        await _probe_poly_pair(p_adapter, pairs[0])

        print("\n  Fetching live quotes...\n")
        results = await price_all_pairs(pairs, p_adapter, k_adapter)

    # Detailed per-pair output
    for i, r in enumerate(results, 1):
        _print_pair_detail(r, i, len(results))

    # Summary table sorted by net edge
    _print_summary_table(results)

    # Paper trading pass
    paper_trader = PaperTrader(db_path=_PAPER_TRADES_PATH, limits=_RISK)
    tradeable = [
        r for r in results
        if r.opportunity and r.opportunity.status == OpportunityStatus.TRADEABLE
    ]

    print()
    if tradeable:
        print("═" * W)
        print(f"  PAPER TRADING — {len(tradeable)} tradeable opportunity(-ies) found")
        print("═" * W)
        for r in sorted(tradeable, key=lambda x: x.opportunity.net_edge, reverse=True):  # type: ignore[union-attr]
            opp = r.opportunity
            assert opp is not None
            print(f"\n  Attempting: {r.approved.kalshi_id}  net={opp.net_edge:+.4f}")
            trade = paper_trader.execute(opp)
            if trade:
                print(
                    f"  SIMULATED FILL #{trade.trade_id}"
                    f"  Poly {trade.poly_side}@{trade.poly_fill_price:.4f}"
                    f"  + Kalshi {trade.kalshi_side}@{trade.kalshi_fill_price:.4f}"
                    f"  = ${trade.estimated_profit_usdc:+.4f} expected profit"
                )
            else:
                print("  SKIPPED (risk controls)")
    else:
        print("  No tradeable opportunities found at current prices.")

    # Final diagnostics summary
    _print_diagnostics_summary(results, paper_trader)

    print(f"\n  Paper trades persisted to: {_PAPER_TRADES_PATH}")
    print("═" * W)


if __name__ == "__main__":
    asyncio.run(main())
