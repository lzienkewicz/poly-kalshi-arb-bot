"""
Exact-market matcher: decides whether a Polymarket market and a Kalshi market
represent the same real-world event and are therefore eligible for arbitrage.

Pipeline
--------
1. Hard rejection rules (cheap, deterministic, fail-fast)
   - Deadline gap > 24 h          → EQUIV_DEADLINE_GAP
   - Resolver mismatch             → EQUIV_RESOLVER_MISMATCH
   - Conditional clause detected   → EQUIV_CONDITIONAL_MISMATCH

2. Semantic equivalence
   - Normalised question strings must match exactly -OR-
     token-overlap Jaccard ≥ JACCARD_EXACT_THRESHOLD (default 0.85)
     and all hard rules pass        → EXACT candidate
   - Jaccard ≥ JACCARD_PROBABLE_THRESHOLD (default 0.60) → PROBABLE
   - Otherwise                     → POSSIBLE / UNKNOWN

3. Allowlist gate
   - Only pairs listed in config/approved_pairs.json may be EXACT.
   - Any pair that passes semantic scoring but is NOT in the allowlist
     is downgraded to PROBABLE and rejected with EQUIV_NOT_IN_APPROVED_PAIRS.

4. Direction inversion detection
   - If the YES sides resolve under opposite scenarios, the Kalshi leg is
     noted as inverted (directions_inverted=True) so the executor flips it.

Output
------
Returns an EventPair with status=MATCHED/REJECTED and confidence EXACT/PROBABLE/…
Only status=MATCHED + confidence=EXACT is tradeable (EventPair.is_tradeable).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import structlog

from src.models.event_pair import EventPair, MatchConfidence, MatchStatus
from src.models.market import Market
from src.normalization import CanonicalMarket, normalize_market
from src.semantic import extract_semantics, is_structural_exact, semantics_compatible

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

_DEADLINE_TOLERANCE = timedelta(hours=24)
_JACCARD_EXACT: float = 0.85
_JACCARD_PROBABLE: float = 0.40

_DEFAULT_PAIRS_PATH = Path(__file__).parent.parent / "config" / "approved_pairs.json"

# ---------------------------------------------------------------------------
# Conditional-clause signals
# Phrases that indicate a market has an extra condition the other may lack.
# ---------------------------------------------------------------------------

_CONDITIONAL_SIGNALS: frozenset[str] = frozenset(
    {
        "if cancelled",
        "if postponed",
        "if suspended",
        "if delayed",
        "barring",
        "unless",
        "contingent",
        "subject to",
        "provided that",
        "assuming",
    }
)

# ---------------------------------------------------------------------------
# Negation signals — detect YES/NO inversion between venues.
# If one question has these and the other does not, the directions are inverted.
# ---------------------------------------------------------------------------

_NEGATION_SIGNALS: frozenset[str] = frozenset(
    {"not", "fail", "fails", "failed", "below", "under", "less than", "no longer"}
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def match(
    poly: Market,
    kalshi: Market,
    *,
    approved_pairs_path: Path = _DEFAULT_PAIRS_PATH,
    deadline_tolerance: timedelta = _DEADLINE_TOLERANCE,
) -> EventPair:
    """Evaluate whether poly and kalshi represent the same real-world event.

    Returns an EventPair; check .is_tradeable before acting on it.
    """
    canon_poly = normalize_market(poly)
    canon_kalshi = normalize_market(kalshi)

    result = _evaluate(canon_poly, canon_kalshi, poly, kalshi, approved_pairs_path, deadline_tolerance)
    log.debug(
        "matcher_result",
        pair_key=result.pair_key,
        status=result.status,
        confidence=result.confidence,
        reject_reason=result.reject_reason,
    )
    return result


def match_all(
    poly_markets: Sequence[Market],
    kalshi_markets: Sequence[Market],
    *,
    approved_pairs_path: Path = _DEFAULT_PAIRS_PATH,
    deadline_window: timedelta = timedelta(hours=48),
) -> list[EventPair]:
    """Cross-match every Polymarket market against every Kalshi market.

    Pre-filters by time window before semantic matching — pairs whose effective
    deadlines differ by more than deadline_window are skipped entirely.  This
    reduces an O(N²) full cross-product to only time-adjacent pairs.

    Returns only pairs with confidence >= PROBABLE (discards POSSIBLE/UNKNOWN).
    """
    # Build a canonical snapshot of each market's effective deadline once,
    # outside the inner loop, so normalize_market isn't called N² times.
    poly_canon = [(normalize_market(m), m) for m in poly_markets]
    kalshi_canon = [(normalize_market(m), m) for m in kalshi_markets]

    results: list[EventPair] = []
    skipped_time = 0

    for cp, poly in poly_canon:
        poly_deadline = cp.resolution_time or cp.close_time
        for ck, kalshi in kalshi_canon:
            kalshi_deadline = ck.resolution_time or ck.close_time
            if abs(poly_deadline - kalshi_deadline) > deadline_window:
                skipped_time += 1
                continue
            pair = match(poly, kalshi, approved_pairs_path=approved_pairs_path)
            if pair.confidence not in (MatchConfidence.POSSIBLE, MatchConfidence.UNKNOWN):
                results.append(pair)

    log.info(
        "matcher_cross_match_done",
        poly=len(poly_markets),
        kalshi=len(kalshi_markets),
        skipped_time=skipped_time,
        kept=len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Core evaluation pipeline
# ---------------------------------------------------------------------------


def _evaluate(
    canon_poly: CanonicalMarket,
    canon_kalshi: CanonicalMarket,
    poly: Market,
    kalshi: Market,
    approved_pairs_path: Path,
    deadline_tolerance: timedelta = _DEADLINE_TOLERANCE,
) -> EventPair:
    def _reject(reason: str, detail: str) -> EventPair:
        return EventPair(
            polymarket=poly,
            kalshi=kalshi,
            status=MatchStatus.REJECTED,
            confidence=MatchConfidence.UNKNOWN,
            reject_reason=reason,
            reject_detail=detail,
        )

    # ── Hard rule: Deadline alignment ─────────────────────────────────────
    poly_deadline = canon_poly.resolution_time or canon_poly.close_time
    kalshi_deadline = canon_kalshi.resolution_time or canon_kalshi.close_time
    deadline_gap = abs(poly_deadline - kalshi_deadline)
    if deadline_gap > deadline_tolerance:
        return _reject(
            "EQUIV_DEADLINE_GAP",
            f"Deadline gap {deadline_gap} exceeds {deadline_tolerance} "
            f"(poly={poly_deadline.isoformat()}, kalshi={kalshi_deadline.isoformat()})",
        )

    # ── Hard rule: Resolver compatibility ─────────────────────────────────
    if not _resolvers_compatible(canon_poly, canon_kalshi):
        return _reject(
            "EQUIV_RESOLVER_MISMATCH",
            f"poly resolver={canon_poly.resolution_source_canonical!r} "
            f"kalshi resolver={canon_kalshi.resolution_source_canonical!r}",
        )

    # ── Hard rule: Conditional clauses ────────────────────────────────────
    poly_conditional = _has_conditional(canon_poly.question_normalized)
    kalshi_conditional = _has_conditional(canon_kalshi.question_normalized)
    if poly_conditional != kalshi_conditional:
        return _reject(
            "EQUIV_CONDITIONAL_MISMATCH",
            f"Conditional clause detected: poly={poly_conditional} kalshi={kalshi_conditional}",
        )

    # ── Approved pairs fast path ──────────────────────────────────────────
    # Pairs in approved_pairs.json have been manually verified as equivalent.
    # They bypass the structural and Jaccard checks below.
    approved = _load_approved_pairs(approved_pairs_path)
    pair_key = f"{poly.venue_market_id}::{kalshi.venue_market_id}"
    if pair_key in approved:
        inverted = _directions_inverted(canon_poly.question_normalized, canon_kalshi.question_normalized)
        return EventPair(
            polymarket=poly,
            kalshi=kalshi,
            status=MatchStatus.MATCHED,
            confidence=MatchConfidence.EXACT,
            directions_inverted=inverted,
        )

    # ── Structural semantic check (entity / event_type / cardinality) ─────
    poly_sem = extract_semantics(poly.question)
    kalshi_sem = extract_semantics(kalshi.question)
    sem_ok, sem_detail = semantics_compatible(poly_sem, kalshi_sem)
    if not sem_ok:
        return _reject("EQUIV_SEMANTIC_MISMATCH", sem_detail)

    # Structural exact: all extracted fields agree positively — promotes PROBABLE → EXACT
    structural_exact = is_structural_exact(poly_sem, kalshi_sem)

    # ── Jaccard-based confidence ───────────────────────────────────────────
    confidence, inverted = _semantic_match(canon_poly, canon_kalshi, structural_exact)

    if confidence == MatchConfidence.UNKNOWN:
        return EventPair(
            polymarket=poly,
            kalshi=kalshi,
            status=MatchStatus.REJECTED,
            confidence=MatchConfidence.UNKNOWN,
            reject_reason="EQUIV_EVENT_MISMATCH",
            reject_detail="No meaningful token overlap",
        )

    if confidence == MatchConfidence.POSSIBLE:
        return EventPair(
            polymarket=poly,
            kalshi=kalshi,
            status=MatchStatus.REJECTED,
            confidence=MatchConfidence.POSSIBLE,
            reject_reason="EQUIV_EVENT_MISMATCH",
            reject_detail="Partial keyword overlap only — below PROBABLE threshold",
        )

    # PROBABLE or EXACT — not in approved_pairs (would have returned above)
    if confidence == MatchConfidence.EXACT:
        if structural_exact:
            # All structured fields agree — keep EXACT confidence for human promotion
            return EventPair(
                polymarket=poly,
                kalshi=kalshi,
                status=MatchStatus.REJECTED,
                confidence=MatchConfidence.EXACT,
                directions_inverted=inverted,
                reject_reason="EQUIV_STRUCTURAL_EXACT_NOT_IN_PAIRS",
                reject_detail=f"Structural exact: add {pair_key!r} to approved_pairs.json after review",
            )
        # Jaccard-only exact — not structurally verified, downgrade to PROBABLE
        return EventPair(
            polymarket=poly,
            kalshi=kalshi,
            status=MatchStatus.REJECTED,
            confidence=MatchConfidence.PROBABLE,
            directions_inverted=inverted,
            reject_reason="EQUIV_NOT_IN_APPROVED_PAIRS",
            reject_detail=f"Pair {pair_key!r} not in approved_pairs.json — add after human review",
        )

    # PROBABLE
    return EventPair(
        polymarket=poly,
        kalshi=kalshi,
        status=MatchStatus.REJECTED,
        confidence=MatchConfidence.PROBABLE,
        directions_inverted=inverted,
        reject_reason="EQUIV_CONFIDENCE_NOT_EXACT",
        reject_detail="Semantic match is PROBABLE but not EXACT — manual review required",
    )


# ---------------------------------------------------------------------------
# Semantic matching helpers
# ---------------------------------------------------------------------------


def _semantic_match(
    canon_poly: CanonicalMarket,
    canon_kalshi: CanonicalMarket,
    structural_exact: bool = False,
) -> tuple[MatchConfidence, bool]:
    """Return (confidence, directions_inverted).

    directions_inverted is True when one question contains negation signals
    that the other does not — implying the YES sides resolve under opposite
    scenarios and the Kalshi leg must be flipped.

    structural_exact=True promotes any non-UNKNOWN result to EXACT so that
    pairs with matching entity/family/party/office/jurisdiction are not held
    back by Jaccard alone (different question wording, same real-world event).
    """
    # Exact normalized string match is the strongest signal
    exact_string = canon_poly.question_normalized == canon_kalshi.question_normalized

    # Token-overlap Jaccard similarity
    tokens_poly = canon_poly.question_tokens
    tokens_kalshi = canon_kalshi.question_tokens
    jaccard = _jaccard(tokens_poly, tokens_kalshi)

    if exact_string or jaccard >= _JACCARD_EXACT:
        confidence = MatchConfidence.EXACT
    elif jaccard >= _JACCARD_PROBABLE:
        confidence = MatchConfidence.PROBABLE
    elif jaccard > 0:
        confidence = MatchConfidence.POSSIBLE
    else:
        confidence = MatchConfidence.UNKNOWN

    # Structural exact promotes POSSIBLE/PROBABLE → EXACT when all fields agree,
    # but only when there is at least some token overlap (zero-overlap = suspicious).
    if structural_exact and confidence not in (MatchConfidence.UNKNOWN,):
        confidence = MatchConfidence.EXACT

    inverted = _directions_inverted(canon_poly.question_normalized, canon_kalshi.question_normalized)
    return confidence, inverted


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _directions_inverted(q_poly: str, q_kalshi: str) -> bool:
    """True if one question has negation signals and the other does not.

    E.g. "Will X fail to reach Y?" vs "Will X reach Y?" are inverted.
    """
    poly_neg = _has_negation(q_poly)
    kalshi_neg = _has_negation(q_kalshi)
    return poly_neg != kalshi_neg


def _has_negation(question_normalized: str) -> bool:
    for signal in _NEGATION_SIGNALS:
        if re.search(r"\b" + re.escape(signal) + r"\b", question_normalized):
            return True
    return False


def _has_conditional(question_normalized: str) -> bool:
    for signal in _CONDITIONAL_SIGNALS:
        if signal in question_normalized:
            return True
    return False


def _resolvers_compatible(a: CanonicalMarket, b: CanonicalMarket) -> bool:
    """Return True when both markets use the same (or compatible) resolver.

    Both None → compatible (resolver information absent on both sides).
    One None, one set → compatible (partial info; don't reject on absence alone).
    Both set → must match.
    """
    ra = a.resolution_source_canonical
    rb = b.resolution_source_canonical
    if ra is None or rb is None:
        return True
    return ra == rb


# ---------------------------------------------------------------------------
# Approved pairs allowlist
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _load_approved_pairs(path: Path) -> frozenset[str]:
    """Load the set of pre-approved pair keys from config/approved_pairs.json.

    Returns an empty frozenset if the file is missing or malformed.
    Each entry must be a dict with "polymarket_id" and "kalshi_id" keys;
    the pair key is "<polymarket_id>::<kalshi_id>".
    """
    if not path.exists():
        log.warning("approved_pairs_file_missing", path=str(path))
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pairs = data.get("pairs", [])
        return frozenset(
            f"{p['polymarket_id']}::{p['kalshi_id']}"
            for p in pairs
            if "polymarket_id" in p and "kalshi_id" in p
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.error("approved_pairs_load_error", path=str(path), error=str(exc))
        return frozenset()
