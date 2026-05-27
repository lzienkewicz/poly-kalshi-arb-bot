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
6. Print final readiness summary: exact/probable/structural-exact by family,
   malformed counts, top-20 approved pairs, top rejection reasons.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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

POLITICS_DEADLINE_WINDOW = timedelta(days=14)
POLITICS_DEADLINE_TOLERANCE = timedelta(days=14)

JACCARD_MIN = 0.10
# For next_leader, lower Jaccard floor so same-country pairs aren't silently dropped.
NEXT_LEADER_JACCARD_MIN = 0.05

FAMILIES = [
    "nomination",
    "ticket",
    "vp_nomination",
    "first_to_declare",
    "endorsement",
    "declare_run",
    "election_participation",
    "general_election_win",
    "next_leader",
    "leave_office",
    "resignation_removal",
    "succession",
    "appointment",
    "legislation",
    "other",
]


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class FamilyResult:
    family: str
    tradeable_exact: list = field(default_factory=list)    # (CandidatePair, EventPair) — in approved_pairs
    structural_exact: list = field(default_factory=list)   # (CandidatePair, EventPair) — structural, needs review
    probable: list = field(default_factory=list)           # (CandidatePair, EventPair)
    rejection_reasons: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _attr_str(sem: MarketSemantics) -> str:
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
    return detail.split(":")[0].strip()


# ---------------------------------------------------------------------------
# next_leader jurisdiction diagnostics
# ---------------------------------------------------------------------------

def _print_next_leader_jurisdictions(p_markets: list, k_markets: list) -> None:
    """Print per-country counts and same-country candidate pairs for next_leader."""
    poly_by_jur: dict[str, list] = defaultdict(list)
    kalshi_by_jur: dict[str, list] = defaultdict(list)

    for m in p_markets:
        jur = extract_semantics(m.question).jurisdiction or "unknown"
        poly_by_jur[jur].append(m)
    for m in k_markets:
        jur = extract_semantics(m.question).jurisdiction or "unknown"
        kalshi_by_jur[jur].append(m)

    all_jurs = sorted(set(poly_by_jur) | set(kalshi_by_jur))
    print("\n  next_leader jurisdiction breakdown:")
    for jur in all_jurs:
        n_p = len(poly_by_jur.get(jur, []))
        n_k = len(kalshi_by_jur.get(jur, []))
        tag = "  <-- both" if n_p and n_k else ""
        print(f"    {jur:20s}  poly={n_p:3d}  kalshi={n_k:3d}{tag}")

    # For each jurisdiction with both venues, show semantically-compatible pairs
    # regardless of Jaccard (Eisenkot/Eizenkot etc. may have low word overlap).
    same_country_pairs = []
    for jur in all_jurs:
        pm_list = poly_by_jur.get(jur, [])
        km_list = kalshi_by_jur.get(jur, [])
        if not pm_list or not km_list or jur == "unknown":
            continue
        for pm in pm_list:
            sem_p = extract_semantics(pm.question)
            for km in km_list:
                sem_k = extract_semantics(km.question)
                ok, _ = semantics_compatible(sem_p, sem_k)
                if ok:
                    same_country_pairs.append((jur, pm, km, sem_p, sem_k))

    if same_country_pairs:
        print(f"\n  Same-country semantic matches ({len(same_country_pairs)} total):")
        for jur, pm, km, sem_p, sem_k in same_country_pairs[:10]:
            print(f"    [{jur}]")
            print(f"      poly  [{_attr_str(sem_p)}]: {pm.question[:60]!r}")
            print(f"      kalshi[{_attr_str(sem_k)}]: {km.question[:60]!r}")


# ---------------------------------------------------------------------------
# Per-family analysis
# ---------------------------------------------------------------------------

def _analyze_family(family: str, p_markets: list, k_markets: list) -> FamilyResult:
    result = FamilyResult(family=family)
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"FAMILY: {family.upper()}  poly={len(p_markets)}  kalshi={len(k_markets)}")
    print(sep)

    if not p_markets or not k_markets:
        print("  (Only one venue has markets in this family — no pairs to evaluate)")
        return result

    jac_min = NEXT_LEADER_JACCARD_MIN if family == "next_leader" else JACCARD_MIN

    # ── next_leader: jurisdiction breakdown ───────────────────────────────
    if family == "next_leader":
        _print_next_leader_jurisdictions(p_markets, k_markets)

    # ── Stage A: time window + pre-Jaccard gate + Jaccard floor ───────────
    candidates, counts = generate_candidates(
        p_markets,
        k_markets,
        domain="all",
        sports_subdomain="all",
        deadline_window=POLITICS_DEADLINE_WINDOW,
        jaccard_min=jac_min,
        now=NOW,
        enable_politics_gates=True,
    )
    print(
        f"  Pre-gate dropped:        {counts.pre_jaccard_gate_dropped}"
        f"  (empty_entity, cardinality, office, jurisdiction, family mismatches)"
    )
    if counts.pre_jaccard_gate_reasons:
        for reason, cnt in sorted(counts.pre_jaccard_gate_reasons.items(), key=lambda x: -x[1]):
            print(f"    {cnt:5d}x  {reason}")
    print(
        f"  Jaccard≥threshold cands: {counts.final_candidates}"
        f"  (time-skipped={counts.time_skipped}, jac-dropped={counts.jaccard_floor_dropped})"
    )

    if not candidates:
        print("  (No Jaccard candidates — questions may use completely different vocabulary)")
        print("\n  Sample poly questions:")
        for m in p_markets[:5]:
            print(f"    {m.question[:70]!r}")
        print("\n  Sample kalshi questions:")
        for m in k_markets[:5]:
            print(f"    {m.question[:70]!r}")
        return result

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

    if sem_blocked:
        reason_counts = Counter(_rejection_category(d) for _, d in sem_blocked)
        print("  Rejection breakdown:")
        for reason, cnt in reason_counts.most_common():
            print(f"    {cnt:4d}x  {reason}")

    if not sem_pass:
        print("\n  (No semantic passes — top 5 blocked pairs for inspection):")
        for c, detail in sem_blocked[:5]:
            sem_a = extract_semantics(c.poly.question)
            sem_b = extract_semantics(c.kalshi.question)
            print(f"    blocked: {detail}")
            print(f"      poly  [{_attr_str(sem_a)}]: {c.poly.question[:58]!r}")
            print(f"      kalshi[{_attr_str(sem_b)}]: {c.kalshi.question[:58]!r}")
        return result

    # ── Stage C: full matcher on semantic passes ───────────────────────────
    display = sem_pass[:20]
    print(f"\n  TOP {len(display)} CANDIDATES  ({len(sem_pass)} total pass):")

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

        if ep.is_tradeable:
            result.tradeable_exact.append((c, ep))
        elif ep.confidence == MatchConfidence.EXACT:
            result.structural_exact.append((c, ep))
        elif ep.confidence == MatchConfidence.PROBABLE:
            result.probable.append((c, ep))
        if ep.reject_reason:
            result.rejection_reasons[ep.reject_reason] += 1

    n_t = len(result.tradeable_exact)
    n_s = len(result.structural_exact)
    n_p = len(result.probable)
    print(f"\n  Summary: EXACT(tradeable)={n_t}  STRUCTURAL_EXACT(review)={n_s}  PROBABLE={n_p}"
          f"  shown={len(display)}")

    if result.structural_exact:
        print("\n  STRUCTURAL EXACT pairs — add to approved_pairs.json after review:")
        for c, ep in result.structural_exact:
            print(f'    {{"polymarket_id": "{c.poly.venue_market_id}", '
                  f'"kalshi_id": "{c.kalshi.venue_market_id}"}}')
            print(f'     poly:   {c.poly.question[:60]!r}')
            print(f'     kalshi: {c.kalshi.question[:60]!r}')

    if result.probable:
        print("\n  PROBABLE pairs — lower confidence, needs manual review:")
        for c, ep in result.probable:
            print(f'    {{"polymarket_id": "{c.poly.venue_market_id}", '
                  f'"kalshi_id": "{c.kalshi.venue_market_id}"}}')
            print(f'     poly:   {c.poly.question[:60]!r}')
            print(f'     kalshi: {c.kalshi.question[:60]!r}')

    return result


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
    all_families = set(poly_by_family) | set(kalshi_by_family)
    for fam in FAMILIES + sorted(all_families - set(FAMILIES)):
        n_p = len(poly_by_family.get(fam, []))
        n_k = len(kalshi_by_family.get(fam, []))
        if n_p + n_k > 0:
            both = "  <-- both" if n_p and n_k else ""
            print(f"  {fam:25s}  {n_p:6d}  {n_k:6d}{both}")
    print(f"  {'TOTAL':25s}  {len(poly_pol):6d}  {len(kalshi_pol):6d}")

    # ── Malformed / empty-entity markets by family ────────────────────────
    poly_empty_by_fam: dict[str, list] = defaultdict(list)
    kalshi_empty_by_fam: dict[str, list] = defaultdict(list)
    for m in poly_pol:
        sem = extract_semantics(m.question)
        if sem.cardinality == 0:
            poly_empty_by_fam[sem.event_type].append(m)
    for m in kalshi_pol:
        sem = extract_semantics(m.question)
        if sem.cardinality == 0:
            kalshi_empty_by_fam[sem.event_type].append(m)

    total_poly_empty = sum(len(v) for v in poly_empty_by_fam.values())
    total_kalshi_empty = sum(len(v) for v in kalshi_empty_by_fam.values())
    print(f"\n  Malformed/empty-entity: poly={total_poly_empty}  kalshi={total_kalshi_empty}")

    if poly_empty_by_fam:
        print("  Poly malformed by family:")
        for fam in FAMILIES + sorted(set(poly_empty_by_fam) - set(FAMILIES)):
            ms = poly_empty_by_fam.get(fam, [])
            if not ms:
                continue
            example = ms[0].question[:70]
            print(f"    {fam:25s}  {len(ms):4d}  e.g. {example!r}")

    if kalshi_empty_by_fam:
        print("  Kalshi malformed by family:")
        for fam in FAMILIES + sorted(set(kalshi_empty_by_fam) - set(FAMILIES)):
            ms = kalshi_empty_by_fam.get(fam, [])
            if not ms:
                continue
            example = ms[0].question[:70]
            print(f"    {fam:25s}  {len(ms):4d}  e.g. {example!r}")

    # ── Per-family analysis ────────────────────────────────────────────────
    analysis_families = FAMILIES + sorted(all_families - set(FAMILIES))
    family_results: list[FamilyResult] = []
    for family in analysis_families:
        p_markets = poly_by_family.get(family, [])
        k_markets = kalshi_by_family.get(family, [])
        if not p_markets and not k_markets:
            continue
        fr = _analyze_family(family, p_markets, k_markets)
        family_results.append(fr)

    # ── Final readiness summary ────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("FINAL READINESS SUMMARY")
    print("=" * 70)

    all_tradeable: list[tuple] = []
    all_structural: list[tuple] = []
    all_probable: list[tuple] = []
    all_rejections: Counter = Counter()

    print(f"\n  {'Family':25s}  {'Exact':>6s}  {'Struct':>6s}  {'Probable':>8s}")
    for fr in family_results:
        n_t = len(fr.tradeable_exact)
        n_s = len(fr.structural_exact)
        n_p = len(fr.probable)
        if n_t + n_s + n_p == 0:
            continue
        print(f"  {fr.family:25s}  {n_t:6d}  {n_s:6d}  {n_p:8d}")
        all_tradeable.extend(fr.tradeable_exact)
        all_structural.extend(fr.structural_exact)
        all_probable.extend(fr.probable)
        all_rejections.update(fr.rejection_reasons)

    print(f"\n  Malformed totals: poly={total_poly_empty}  kalshi={total_kalshi_empty}")
    print(f"  Tradeable exact pairs (in approved_pairs): {len(all_tradeable)}")
    print(f"  Structural exact pairs (pending review):   {len(all_structural)}")
    print(f"  Probable pairs (lower confidence):         {len(all_probable)}")

    if all_tradeable:
        print(f"\n  TOP {min(20, len(all_tradeable))} APPROVED EXACT PAIRS:")
        for c, _ in all_tradeable[:20]:
            print(f'    poly:   {c.poly.question[:55]!r}')
            print(f'    kalshi: {c.kalshi.question[:55]!r}')
            print()

    if all_rejections:
        print("  TOP REJECTION REASONS (across all families):")
        for reason, cnt in all_rejections.most_common(10):
            print(f"    {cnt:5d}x  {reason}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


asyncio.run(main())
