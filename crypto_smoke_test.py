"""
Diagnostic: crypto domain — per-family market counts, candidate pairs, and
matcher results.

Pipeline
--------
1. Fetch all open Kalshi (Crypto / Finance) and Polymarket markets.
2. Filter to simple (non-combo) crypto markets via classify_domain.
3. Route each market to a crypto event family via extract_crypto_semantics.
4. For each family with markets on both venues:
   a. Time-window + Jaccard pre-filter  (generate_candidates, domain=all)
   b. Crypto semantic filter            (crypto_semantics_compatible)
   c. Full matcher                      (match)
5. Print per-family counts, rejection breakdowns, top-20 candidates,
   and 20 sample markets with extracted fields.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from src.candidates import classify_domain, generate_candidates, is_combo_market
from src.crypto_semantic import (
    CryptoSemantics,
    crypto_semantics_compatible,
    extract_crypto_semantics,
)
from src.matcher import match
from src.models.event_pair import MatchConfidence
from src.venues.kalshi.adapter import KalshiAdapter
from src.venues.kalshi.client import KalshiClient
from src.venues.polymarket.adapter import PolymarketAdapter
from src.venues.polymarket.client import PolymarketClient

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

NOW = datetime.now(timezone.utc)

# Kalshi event categories that contain crypto markets
KALSHI_CRYPTO_CATEGORIES = [
    "Crypto",
    "Cryptocurrency",
    "Bitcoin",
    "Ethereum",
    "Technology",
    "Finance",
    "Digital Assets",
]

# Time window for crypto deadline pre-filter — crypto markets can have
# close dates days apart for the same underlying event
CRYPTO_DEADLINE_WINDOW = timedelta(days=7)
CRYPTO_DEADLINE_TOLERANCE = timedelta(days=7)

# Jaccard floor — semantic layer does the heavy filtering
JACCARD_MIN = 0.10

# Display order for event families
FAMILIES = [
    "touch_target_by_date",
    "above_below_on_date",
    "ETF_approval",
    "protocol_event",
    "other",
]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _sem_str(sem: CryptoSemantics) -> str:
    """Compact one-liner of crypto semantic fields for diagnostic output."""
    parts: list[str] = []
    if sem.asset:
        parts.append(f"asset={sem.asset}")
    if sem.comparator:
        parts.append(f"cmp={sem.comparator}")
    if sem.threshold is not None:
        parts.append(f"thr={sem.threshold:,.0f}")
    if sem.deadline:
        parts.append(f"dl={sem.deadline}")
    if sem.approval_target:
        parts.append(f"etf={sem.approval_target}")
    if sem.jurisdiction:
        parts.append(f"jur={sem.jurisdiction}")
    if sem.has_conditional:
        parts.append("CONDITIONAL")
    if not parts:
        parts.append(f"family={sem.event_family}")
    return ", ".join(parts)


def _rejection_category(detail: str) -> str:
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
        domain="all",
        sports_subdomain="all",
        deadline_window=CRYPTO_DEADLINE_WINDOW,
        jaccard_min=JACCARD_MIN,
        now=NOW,
    )
    print(
        f"  Jaccard>={JACCARD_MIN} candidates: {counts.final_candidates}"
        f"  (time-skipped={counts.time_skipped}, jac-dropped={counts.jaccard_floor_dropped})"
    )

    if not candidates:
        print("  (No Jaccard candidates — questions may use very different vocabulary)")
        print("\n  Sample poly questions:")
        for m in p_markets[:5]:
            print(f"    {m.question[:72]!r}")
        print("\n  Sample kalshi questions:")
        for m in k_markets[:5]:
            print(f"    {m.question[:72]!r}")
        return

    # ── Stage B: crypto semantic filter ───────────────────────────────────
    sem_pass: list = []
    sem_blocked: list[tuple] = []
    for c in candidates:
        sa = extract_crypto_semantics(c.poly.question)
        sb = extract_crypto_semantics(c.kalshi.question)
        ok, detail = crypto_semantics_compatible(sa, sb)
        if ok:
            sem_pass.append(c)
        else:
            sem_blocked.append((c, detail))

    print(
        f"  Semantic filter:         {len(sem_pass)} pass  /  {len(sem_blocked)} blocked"
    )

    if sem_blocked:
        reason_counts = Counter(_rejection_category(d) for _, d in sem_blocked)
        print("  Rejection breakdown:")
        for reason, cnt in reason_counts.most_common():
            print(f"    {cnt:4d}x  {reason}")

    if not sem_pass:
        print("\n  (No semantic passes — top 5 blocked pairs for inspection):")
        for c, detail in sem_blocked[:5]:
            sa = extract_crypto_semantics(c.poly.question)
            sb = extract_crypto_semantics(c.kalshi.question)
            print(f"    blocked: {detail}")
            print(f"      poly  [{_sem_str(sa)}]: {c.poly.question[:58]!r}")
            print(f"      kalshi[{_sem_str(sb)}]: {c.kalshi.question[:58]!r}")
        return

    # ── Stage C: full matcher ─────────────────────────────────────────────
    display = sem_pass[:20]
    exact_hits: list = []
    probable_hits: list = []

    print(f"\n  TOP {len(display)} CANDIDATES  ({len(sem_pass)} total pass):")
    for i, c in enumerate(display, 1):
        sa = extract_crypto_semantics(c.poly.question)
        sb = extract_crypto_semantics(c.kalshi.question)
        ep = match(c.poly, c.kalshi, deadline_tolerance=CRYPTO_DEADLINE_TOLERANCE)
        conf = ep.confidence.value.upper()
        reason = ep.reject_reason or "OK"
        inv = " [INVERTED]" if ep.directions_inverted else ""
        print(f"\n  #{i:02d}  J={c.jaccard:.3f}  [{conf}] {reason}{inv}")
        print(f"    poly  [{_sem_str(sa)}]: {c.poly.question[:60]!r}")
        print(f"    kalshi[{_sem_str(sb)}]: {c.kalshi.question[:60]!r}")
        if ep.reject_detail:
            print(f"    detail: {ep.reject_detail[:70]}")
        if ep.confidence == MatchConfidence.EXACT and ep.is_tradeable:
            exact_hits.append((c, ep))
        elif ep.confidence == MatchConfidence.PROBABLE:
            probable_hits.append((c, ep))

    print(
        f"\n  Summary: EXACT(tradeable)={len(exact_hits)}"
        f"  PROBABLE={len(probable_hits)}"
        f"  shown={len(display)}"
    )

    if probable_hits:
        print("\n  PROBABLE pairs — add to config/approved_pairs.json after human review:")
        for c, ep in probable_hits:
            print(
                f'    {{"polymarket_id": "{c.poly.venue_market_id}", '
                f'"kalshi_id": "{c.kalshi.venue_market_id}"}}'
            )
            print(f'     poly:   {c.poly.question[:60]!r}')
            print(f'     kalshi: {c.kalshi.question[:60]!r}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("Fetching markets...\n", flush=True)
    async with httpx.AsyncClient() as http:
        kalshi_adapter = KalshiAdapter(KalshiClient(_http=http))
        print(f"  Kalshi categories: {KALSHI_CRYPTO_CATEGORIES}")
        kalshi_markets = await kalshi_adapter.fetch_markets_for_event_categories(
            KALSHI_CRYPTO_CATEGORIES,
            max_events=500,
        )
        poly_markets = await PolymarketAdapter(PolymarketClient(_http=http)).fetch_open_markets()

    print(f"\n  Kalshi total fetched: {len(kalshi_markets)}")
    print(f"  Polymarket total fetched: {len(poly_markets)}\n")

    # Filter to simple crypto markets
    kalshi_crypto = [
        m for m in kalshi_markets
        if classify_domain(m) == "crypto" and not is_combo_market(m)
    ]
    poly_crypto = [
        m for m in poly_markets
        if classify_domain(m) == "crypto" and not is_combo_market(m)
    ]
    print(f"  Simple crypto: Kalshi={len(kalshi_crypto)}  Poly={len(poly_crypto)}\n")

    if not kalshi_crypto and not poly_crypto:
        print(
            "  No crypto markets found. Kalshi categories tried: "
            f"{KALSHI_CRYPTO_CATEGORIES}\n"
            "  Check that category names match what Kalshi uses.\n"
            "  You can also try: fetch_open_markets() and filter client-side."
        )
        return

    # ── Sample markets with extracted semantics ────────────────────────────
    print("=" * 70)
    print("SAMPLE: 20 Kalshi crypto markets with extracted semantics")
    print("=" * 70)
    for m in kalshi_crypto[:20]:
        sem = extract_crypto_semantics(m.question)
        print(f"  [{sem.event_family:22s}] [{_sem_str(sem)[:40]}]")
        print(f"    {m.question[:70]!r}")

    print()
    print("=" * 70)
    print("SAMPLE: 20 Polymarket crypto markets with extracted semantics")
    print("=" * 70)
    for m in poly_crypto[:20]:
        sem = extract_crypto_semantics(m.question)
        print(f"  [{sem.event_family:22s}] [{_sem_str(sem)[:40]}]")
        print(f"    {m.question[:70]!r}")

    # ── Diagnostic: Kalshi markets with incomplete extraction ──────────────
    incomplete = [
        m for m in kalshi_crypto
        if extract_crypto_semantics(m.question).comparator is None
        or extract_crypto_semantics(m.question).threshold is None
    ]
    if incomplete:
        print()
        print("=" * 70)
        print(f"DIAGNOSTIC: {len(incomplete)} Kalshi crypto markets with missing comparator/threshold")
        print("(showing up to 20 — raw API fields used to build the question)")
        print("=" * 70)
        for m in incomplete[:20]:
            sem = extract_crypto_semantics(m.question)
            raw = m.raw
            print(f"\n  ticker:        {raw.get('ticker')}")
            print(f"  title:         {raw.get('title')!r}")
            print(f"  subtitle:      {raw.get('subtitle')!r}")
            print(f"  yes_sub_title: {raw.get('yes_sub_title')!r}")
            print(f"  question used: {m.question!r}")
            print(f"  extracted:     [{_sem_str(sem)}]")

    # Route each market to a crypto event family
    kalshi_by_family: dict[str, list] = defaultdict(list)
    poly_by_family: dict[str, list] = defaultdict(list)

    for m in kalshi_crypto:
        fam = extract_crypto_semantics(m.question).event_family
        kalshi_by_family[fam].append(m)

    for m in poly_crypto:
        fam = extract_crypto_semantics(m.question).event_family
        poly_by_family[fam].append(m)

    # ── Per-family counts table ────────────────────────────────────────────
    print()
    print("=" * 70)
    print("MARKET COUNTS BY CRYPTO EVENT FAMILY")
    print("=" * 70)
    print(f"  {'Family':25s}  {'Poly':>6s}  {'Kalshi':>6s}")
    for fam in FAMILIES:
        n_p = len(poly_by_family.get(fam, []))
        n_k = len(kalshi_by_family.get(fam, []))
        if n_p + n_k > 0:
            both = "  <-- both" if n_p and n_k else ""
            print(f"  {fam:25s}  {n_p:6d}  {n_k:6d}{both}")
    print(f"  {'TOTAL':25s}  {len(poly_crypto):6d}  {len(kalshi_crypto):6d}")

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
