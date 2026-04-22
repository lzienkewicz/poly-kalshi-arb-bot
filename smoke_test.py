"""Diagnostic: politics domain — fetch, classify, candidate pairs, matcher results."""
import asyncio
import logging
from collections import Counter
from datetime import timezone, datetime

import httpx
import structlog
from datetime import timedelta

from src.candidates import (
    MatchDomain,
    classify_domain,
    generate_candidates,
    is_combo_market,
)
from src.semantic import extract_semantics, semantics_compatible
from src.matcher import match
from src.models.event_pair import MatchConfidence
from src.venues.kalshi.adapter import KalshiAdapter
from src.venues.kalshi.client import KalshiClient
from src.venues.polymarket.adapter import PolymarketAdapter
from src.venues.polymarket.client import PolymarketClient

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

MATCH_DOMAIN: MatchDomain = "politics"
POLITICS_DEADLINE_TOLERANCE = timedelta(days=14)
NOW = datetime.now(timezone.utc)

# Kalshi event categories to include (fetched via events endpoint, filtered client-side)
KALSHI_CATEGORIES = ["Politics", "Elections"]


def _days_bucket(dt: datetime) -> str:
    d = (dt - NOW).total_seconds() / 86400
    if d < 0:   return "PAST"
    if d < 2:   return "<2d"
    if d < 7:   return "2-7d"
    if d < 30:  return "7-30d"
    if d < 365: return "30-365d"
    return ">1yr"


async def main():
    print("Fetching markets...\n", flush=True)
    async with httpx.AsyncClient() as http:
        kalshi_adapter = KalshiAdapter(KalshiClient(_http=http))
        print(f"  Fetching Kalshi events for categories: {KALSHI_CATEGORIES}")
        kalshi_markets = await kalshi_adapter.fetch_markets_for_event_categories(KALSHI_CATEGORIES)
        poly_markets = await PolymarketAdapter(PolymarketClient(_http=http)).fetch_open_markets()

    print(f"\nKalshi (politics/elections events): {len(kalshi_markets)}"
          f"   Polymarket: {len(poly_markets)}\n")

    # ── Close-time distributions ───────────────────────────────────────────
    for label, markets in [("Kalshi", kalshi_markets), ("Polymarket", poly_markets)]:
        buckets: Counter = Counter(_days_bucket(m.close_time) for m in markets)
        print(f"{label} close_time distribution:")
        for b in ["<2d", "2-7d", "7-30d", "30-365d", ">1yr", "PAST"]:
            if buckets[b]:
                print(f"  {b:10s}: {buckets[b]}")
        print()

    # ── Politics counts ────────────────────────────────────────────────────
    print("=" * 70)
    print("POLITICS MARKET COUNTS")
    print("=" * 70)
    for label, markets in [("Kalshi", kalshi_markets), ("Polymarket", poly_markets)]:
        pol = [m for m in markets if classify_domain(m) == "politics"]
        combo = sum(1 for m in pol if is_combo_market(m))
        simple = len(pol) - combo
        print(f"\n{label}: {len(pol)} politics  ({simple} simple, {combo} combos)"
              f"  out of {len(markets)} total")

    # ── 40-sample: each venue's politics markets ───────────────────────────
    print("\n" + "=" * 70)
    print("SAMPLE: Kalshi politics simple (up to 40)")
    print("=" * 70)
    kalshi_pol = [m for m in kalshi_markets if classify_domain(m) == "politics" and not is_combo_market(m)]
    for m in kalshi_pol[:40]:
        print(f"  {_days_bucket(m.close_time):6s}  {m.question[:72]!r}")

    print("\n" + "=" * 70)
    print("SAMPLE: Polymarket politics simple (up to 40)")
    print("=" * 70)
    poly_pol = [m for m in poly_markets if classify_domain(m) == "politics" and not is_combo_market(m)]
    for m in poly_pol[:40]:
        print(f"  {_days_bucket(m.close_time):6s}  {m.question[:72]!r}")

    # ── Candidate pipeline ─────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"CANDIDATE PIPELINE  (domain={MATCH_DOMAIN}, window=48h, Jaccard>=0.15)")
    print("=" * 70)

    candidates, counts = generate_candidates(
        poly_markets, kalshi_markets,
        domain=MATCH_DOMAIN,
        now=NOW,
    )

    print(f"\n  Stage 1  fetched       poly={counts.poly_fetched:5d}  kalshi={counts.kalshi_fetched:5d}")
    print(f"  Stage 3  domain        poly={counts.poly_domain:5d}  kalshi={counts.kalshi_domain:5d}")
    print(f"  Stage 4  simple        poly={counts.poly_simple:5d}  kalshi={counts.kalshi_simple:5d}")
    print(f"  Stage 5  48h window    {counts.time_skipped} pairs skipped")
    print(f"  Stage 6  Jaccard>=0.15 {counts.jaccard_floor_dropped} pairs dropped")
    print(f"  Final candidates:      {counts.final_candidates}")

    sem_candidates = [
        c for c in candidates
        if semantics_compatible(extract_semantics(c.poly.question), extract_semantics(c.kalshi.question))[0]
    ]
    print(f"  Stage 6.5 semantic     {len(sem_candidates)} pass / {len(candidates) - len(sem_candidates)} blocked")

    if not candidates:
        print("\n  No candidates — check samples above for overlapping questions.")
        return

    # ── Top 20 semantically-compatible candidates ──────────────────────────
    display = sem_candidates[:20]
    print(f"\n{'=' * 70}")
    print(f"TOP {len(display)} SEMANTIC CANDIDATES by Jaccard  ({len(sem_candidates)} total)")
    print("=" * 70)
    for i, c in enumerate(display, 1):
        print(f"\n  #{i:02d}  Jaccard={c.jaccard:.3f}  poly={c.poly_days:.1f}d  kalshi={c.kalshi_days:.1f}d")
        print(f"  poly:   {c.poly.question[:70]!r}")
        print(f"  kalshi: {c.kalshi.question[:70]!r}")
        print(f"  ids:    poly={c.poly.venue_market_id}  kalshi={c.kalshi.venue_market_id}")

    # ── Matcher results ────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"MATCHER RESULTS  (top {len(display)} semantic candidates)")
    print("=" * 70)

    probable_hits = []
    rejected: list[tuple] = []
    for c in display:
        ep = match(c.poly, c.kalshi, deadline_tolerance=POLITICS_DEADLINE_TOLERANCE)
        conf = ep.confidence.value.upper()
        reason = ep.reject_reason or "OK"
        inv = " INVERTED" if ep.directions_inverted else ""
        print(f"  [{conf:8s}]  J={c.jaccard:.3f}  {reason}{inv}")
        print(f"    poly:   {c.poly.question[:62]!r}")
        print(f"    kalshi: {c.kalshi.question[:62]!r}")
        if ep.confidence == MatchConfidence.PROBABLE:
            probable_hits.append((c, ep))
        elif ep.reject_reason:
            rejected.append((c, ep))

    exact = [c for c in display if match(c.poly, c.kalshi, deadline_tolerance=POLITICS_DEADLINE_TOLERANCE).confidence == MatchConfidence.EXACT]
    print(f"\nSummary: EXACT={len(exact)}  PROBABLE={len(probable_hits)}  candidates_shown={len(display)}")

    # ── Top 10 rejected pairs with reasons ────────────────────────────────
    if rejected:
        print(f"\n{'=' * 70}")
        print(f"TOP {min(10, len(rejected))} REJECTED PAIRS (with reasons)")
        print("=" * 70)
        for c, ep in rejected[:10]:
            print(f"\n  reason: {ep.reject_reason}  detail: {ep.reject_detail or '—'}")
            print(f"  poly:   {c.poly.question[:65]!r}")
            print(f"  kalshi: {c.kalshi.question[:65]!r}")

    if probable_hits:
        print("\nPROBABLE pairs — add to config/approved_pairs.json after human review:")
        for c, ep in probable_hits:
            print(f'  {{"polymarket_id": "{c.poly.venue_market_id}", "kalshi_id": "{c.kalshi.venue_market_id}"}}')
            print(f'   poly:   {c.poly.question[:65]!r}')
            print(f'   kalshi: {c.kalshi.question[:65]!r}')


asyncio.run(main())
