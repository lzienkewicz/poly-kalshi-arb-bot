"""
Diagnostic: politics domain — per-family market counts, candidate pairs, and
matcher results.

Pipeline
--------
1. Fetch all open Kalshi (Politics/Elections) and Polymarket markets.
2. Filter to simple (non-combo) politics markets via classify_domain.
3. Route each market to an event family via extract_semantics.event_type.
4. For each family with markets on both venues:
   a. Time-window + Jaccard pre-filter  (generate_candidates, domain=all)
   b. Semantic filter                   (semantics_compatible)
   c. Full matcher                      (match)
5. Print per-family counts, rejection breakdowns, and top-20 candidates.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from src.candidates import classify_domain, generate_candidates, is_combo_market
from src.matcher import match
from src.models.event_pair import MatchConfidence
from src.semantic import MarketSemantics, extract_semantics, semantics_compatible
from src.venues.kalshi.adapter import KalshiAdapter
from src.venues.kalshi.client import KalshiClient
from src.venues.polymarket.adapter import PolymarketAdapter
from src.venues.polymarket.client import PolymarketClient

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

NOW = datetime.now(timezone.utc)
KALSHI_CATEGORIES = ["Politics", "Elections"]

# Wider time window for politics — election markets can close on the same day
# even if the Jaccard pre-filter window is loose.
POLITICS_DEADLINE_WINDOW = timedelta(days=14)
POLITICS_DEADLINE_TOLERANCE = timedelta(days=14)

# Jaccard floor is intentionally low — semantic layer does the heavy filtering.
JACCARD_MIN = 0.10

# Display order for event families
FAMILIES = [
    "nomination",
    "ticket",
    "vp_nomination",
    "general_election_win",
    "next_leader",
    "leave_office",
    "appointment",
    "legislation",
    "other",
]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _attr_str(sem: MarketSemantics) -> str:
    """Compact one-liner of extracted attributes for diagnostic output."""
    parts: list[str] = []
    if sem.entities:
        parts.append(f"ent={sorted(sem.entities)}")
    if sem.party:
        parts.append(f"party={sem.party}")
    if sem.office:
        parts.append(f"office={sem.office}")
    if sem.jurisdiction:
        parts.append(f"jur={sem.jurisdiction}")
    if sem.departure_method:
        parts.append(f"method={sem.departure_method}")
    if not parts:
        parts.append(f"card={sem.cardinality}")
    return ", ".join(parts)


def _rejection_category(detail: str) -> str:
    """Reduce a detail string to its leading noun for grouping."""
    return detail.split(":")[0].strip()


# ---------------------------------------------------------------------------
# Per-family analysis
# ---------------------------------------------------------------------------

def _analyze_family(family: str, p_markets: list, k_markets: list) -> None:
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"FAMILY: {family.upper()}  poly={len(p_markets)}  kalshi={len(k_markets)}")
    print(sep)

    if not p_markets or not k_markets:
        print("  (Only one venue has markets in this family — no pairs to evaluate)")
        return

    # ── Stage A: time window + Jaccard pre-filter ──────────────────────────
    candidates, counts = generate_candidates(
        p_markets,
        k_markets,
        domain="all",           # already filtered by family
        sports_subdomain="all",
        deadline_window=POLITICS_DEADLINE_WINDOW,
        jaccard_min=JACCARD_MIN,
        now=NOW,
    )
    print(
        f"  Jaccard≥{JACCARD_MIN} candidates: {counts.final_candidates}"
        f"  (time-skipped={counts.time_skipped}, jac-dropped={counts.jaccard_floor_dropped})"
    )

    if not candidates:
        print("  (No Jaccard candidates — questions may use completely different vocabulary)")
        # Sample each venue so the user can diagnose
        print("\n  Sample poly questions:")
        for m in p_markets[:5]:
            print(f"    {m.question[:70]!r}")
        print("\n  Sample kalshi questions:")
        for m in k_markets[:5]:
            print(f"    {m.question[:70]!r}")
        return

    # ── Stage B: semantic filter ───────────────────────────────────────────
    sem_pass: list = []
    sem_blocked: list[tuple] = []
    for c in candidates:
        sem_a = extract_semantics(c.poly.question)
        sem_b = extract_semantics(c.kalshi.question)
        ok, detail = semantics_compatible(sem_a, sem_b)
        if ok:
            sem_pass.append(c)
        else:
            sem_blocked.append((c, detail))

    print(
        f"  Semantic filter:         {len(sem_pass)} pass  /  {len(sem_blocked)} blocked"
    )

    # Rejection breakdown
    if sem_blocked:
        reason_counts = Counter(_rejection_category(d) for _, d in sem_blocked)
        print("  Rejection breakdown:")
        for reason, cnt in reason_counts.most_common():
            print(f"    {cnt:4d}x  {reason}")

    # Diagnose: show top-5 blocked pairs if nothing passed
    if not sem_pass:
        print("\n  (No semantic passes — top 5 blocked pairs for inspection):")
        for c, detail in sem_blocked[:5]:
            sem_a = extract_semantics(c.poly.question)
            sem_b = extract_semantics(c.kalshi.question)
            print(f"    blocked: {detail}")
            print(f"      poly  [{_attr_str(sem_a)}]: {c.poly.question[:58]!r}")
            print(f"      kalshi[{_attr_str(sem_b)}]: {c.kalshi.question[:58]!r}")
        return

    # ── Stage C: full matcher on semantic passes ───────────────────────────
    display = sem_pass[:20]
    print(f"\n  TOP {len(display)} CANDIDATES  ({len(sem_pass)} total pass):")

    probable_hits: list = []
    exact_hits: list = []

    for i, c in enumerate(display, 1):
        sem_a = extract_semantics(c.poly.question)
        sem_b = extract_semantics(c.kalshi.question)
        ep = match(c.poly, c.kalshi, deadline_tolerance=POLITICS_DEADLINE_TOLERANCE)
        conf = ep.confidence.value.upper()
        reason = ep.reject_reason or "OK"
        inv = " [INVERTED]" if ep.directions_inverted else ""
        print(f"\n  #{i:02d}  J={c.jaccard:.3f}  [{conf}] {reason}{inv}")
        print(f"    poly  [{_attr_str(sem_a)}]: {c.poly.question[:60]!r}")
        print(f"    kalshi[{_attr_str(sem_b)}]: {c.kalshi.question[:60]!r}")
        if ep.reject_detail:
            print(f"    detail: {ep.reject_detail[:70]}")

        if ep.confidence == MatchConfidence.EXACT and ep.is_tradeable:
            exact_hits.append((c, ep))
        elif ep.confidence == MatchConfidence.PROBABLE:
            probable_hits.append((c, ep))

    # Summary
    print(f"\n  Summary: EXACT(tradeable)={len(exact_hits)}  PROBABLE={len(probable_hits)}"
          f"  shown={len(display)}")

    if probable_hits:
        print("\n  PROBABLE pairs — add to config/approved_pairs.json after human review:")
        for c, ep in probable_hits:
            print(f'    {{"polymarket_id": "{c.poly.venue_market_id}", '
                  f'"kalshi_id": "{c.kalshi.venue_market_id}"}}')
            print(f'     poly:   {c.poly.question[:60]!r}')
            print(f'     kalshi: {c.kalshi.question[:60]!r}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("Fetching markets...\n", flush=True)
    async with httpx.AsyncClient() as http:
        kalshi_adapter = KalshiAdapter(KalshiClient(_http=http))
        print(f"  Kalshi categories: {KALSHI_CATEGORIES}")
        kalshi_markets = await kalshi_adapter.fetch_markets_for_event_categories(KALSHI_CATEGORIES)
        poly_markets = await PolymarketAdapter(PolymarketClient(_http=http)).fetch_open_markets()

    print(f"\n  Kalshi total: {len(kalshi_markets)}   Polymarket total: {len(poly_markets)}\n")

    # Filter to simple politics markets
    kalshi_pol = [
        m for m in kalshi_markets
        if classify_domain(m) == "politics" and not is_combo_market(m)
    ]
    poly_pol = [
        m for m in poly_markets
        if classify_domain(m) == "politics" and not is_combo_market(m)
    ]
    print(f"  Simple politics: Kalshi={len(kalshi_pol)}  Poly={len(poly_pol)}\n")

    # Route each market to an event family
    kalshi_by_family: dict[str, list] = defaultdict(list)
    poly_by_family: dict[str, list] = defaultdict(list)

    for m in kalshi_pol:
        fam = extract_semantics(m.question).event_type
        kalshi_by_family[fam].append(m)

    for m in poly_pol:
        fam = extract_semantics(m.question).event_type
        poly_by_family[fam].append(m)

    # ── Per-family counts table ────────────────────────────────────────────
    print("=" * 70)
    print("MARKET COUNTS BY EVENT FAMILY")
    print("=" * 70)
    print(f"  {'Family':25s}  {'Poly':>6s}  {'Kalshi':>6s}")
    for fam in FAMILIES:
        n_p = len(poly_by_family.get(fam, []))
        n_k = len(kalshi_by_family.get(fam, []))
        if n_p + n_k > 0:
            both = "  <-- both" if n_p and n_k else ""
            print(f"  {fam:25s}  {n_p:6d}  {n_k:6d}{both}")
    print(f"  {'TOTAL':25s}  {len(poly_pol):6d}  {len(kalshi_pol):6d}")

    # ── Per-family analysis ────────────────────────────────────────────────
    for family in FAMILIES:
        p_markets = poly_by_family.get(family, [])
        k_markets = kalshi_by_family.get(family, [])
        if not p_markets and not k_markets:
            continue
        _analyze_family(family, p_markets, k_markets)

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


asyncio.run(main())
