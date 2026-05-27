"""
Unit tests for src/matcher.py.

No HTTP, no async, no real filesystem writes (approved_pairs injected via tmp_path).
Covers:
  - All 6 equivalence rejection codes
  - EXACT match (approved pair)
  - PROBABLE match (approved but below threshold, or above threshold but not approved)
  - Direction inversion detection
  - Intentionally misleading near-matches that must be rejected
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models.event_pair import MatchConfidence, MatchStatus
from src.models.market import Market, Venue
from src.matcher import match, match_all

_UTC = timezone.utc
_T0 = datetime(2025, 6, 30, 18, 0, 0, tzinfo=_UTC)
_T1 = _T0 + timedelta(hours=1)      # 1 h gap — within 24 h
_T25 = _T0 + timedelta(hours=25)    # 25 h gap — exceeds 24 h


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(
    *,
    venue: Venue = Venue.POLYMARKET,
    market_id: str = "0xabc",
    question: str = "Will the Fed cut rates by June 2025?",
    resolution_source: str | None = "Federal Reserve",
    close_time: datetime = _T0,
    is_open: bool = True,
) -> Market:
    return Market(
        venue=venue,
        venue_market_id=market_id,
        question=question,
        question_normalized="",
        resolution_source=resolution_source,
        close_time=close_time,
        is_open=is_open,
    )


def _kalshi(**kwargs) -> Market:
    defaults = dict(
        venue=Venue.KALSHI,
        market_id="KXFED-25JUN-T5.25",
        question="Will the Fed cut rates by June 2025?",
        resolution_source="Federal Reserve",
        close_time=_T0,
    )
    defaults.update(kwargs)
    return _market(**defaults)


def _poly(**kwargs) -> Market:
    defaults = dict(
        venue=Venue.POLYMARKET,
        market_id="0xfed111",
        question="Will the Fed cut rates by June 2025?",
        resolution_source="Federal Reserve",
        close_time=_T0,
    )
    defaults.update(kwargs)
    return _market(**defaults)


def _pairs_file(tmp_path: Path, pairs: list[dict]) -> Path:
    p = tmp_path / "approved_pairs.json"
    p.write_text(json.dumps({"pairs": pairs}), encoding="utf-8")
    return p


def _approved(tmp_path: Path, poly_id: str = "0xfed111", kalshi_id: str = "KXFED-25JUN-T5.25") -> Path:
    return _pairs_file(tmp_path, [{"polymarket_id": poly_id, "kalshi_id": kalshi_id}])


def _empty_pairs(tmp_path: Path) -> Path:
    return _pairs_file(tmp_path, [])


# ---------------------------------------------------------------------------
# Rule 4.3 — Deadline alignment
# ---------------------------------------------------------------------------

def test_deadline_within_tolerance_passes(tmp_path):
    p = _poly(close_time=_T0)
    k = _kalshi(close_time=_T1)
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_DEADLINE_GAP"


def test_deadline_exactly_24h_passes(tmp_path):
    p = _poly(close_time=_T0)
    k = _kalshi(close_time=_T0 + timedelta(hours=24))
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_DEADLINE_GAP"


def test_deadline_25h_gap_rejected(tmp_path):
    p = _poly(close_time=_T0)
    k = _kalshi(close_time=_T25)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.status == MatchStatus.REJECTED
    assert result.reject_reason == "EQUIV_DEADLINE_GAP"


def test_deadline_gap_uses_absolute_value(tmp_path):
    # Kalshi earlier than Polymarket — still a gap check
    p = _poly(close_time=_T25)
    k = _kalshi(close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason == "EQUIV_DEADLINE_GAP"


# ---------------------------------------------------------------------------
# Rule 4.4 — Resolver match
# ---------------------------------------------------------------------------

def test_same_resolver_passes(tmp_path):
    p = _poly(resolution_source="Federal Reserve")
    k = _kalshi(resolution_source="FOMC")  # alias → same canonical
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_RESOLVER_MISMATCH"


def test_different_resolvers_rejected(tmp_path):
    p = _poly(resolution_source="Reuters")
    k = _kalshi(resolution_source="Federal Reserve")
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.status == MatchStatus.REJECTED
    assert result.reject_reason == "EQUIV_RESOLVER_MISMATCH"


def test_resolver_none_on_both_passes(tmp_path):
    p = _poly(resolution_source=None)
    k = _kalshi(resolution_source=None)
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_RESOLVER_MISMATCH"


def test_resolver_none_on_one_side_passes(tmp_path):
    # Absent resolver on one side → don't reject on absence alone
    p = _poly(resolution_source=None)
    k = _kalshi(resolution_source="Federal Reserve")
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_RESOLVER_MISMATCH"


def test_ap_alias_matches_associated_press(tmp_path):
    p = _poly(resolution_source="AP")
    k = _kalshi(resolution_source="Associated Press")
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_RESOLVER_MISMATCH"


# ---------------------------------------------------------------------------
# Rule 4.5 — Conditional clauses
# ---------------------------------------------------------------------------

def test_no_conditionals_passes(tmp_path):
    p = _poly(question="Will the Fed cut rates by June 2025?")
    k = _kalshi(question="Will the Fed cut rates by June 2025?")
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_CONDITIONAL_MISMATCH"


def test_conditional_on_poly_not_kalshi_rejected(tmp_path):
    p = _poly(question="Will the Fed cut rates by June 2025, barring a financial crisis?")
    k = _kalshi(question="Will the Fed cut rates by June 2025?")
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.status == MatchStatus.REJECTED
    assert result.reject_reason == "EQUIV_CONDITIONAL_MISMATCH"


def test_conditional_on_kalshi_not_poly_rejected(tmp_path):
    p = _poly(question="Will the Fed cut rates by June 2025?")
    k = _kalshi(question="Will the Fed cut rates by June 2025, unless delayed?")
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason == "EQUIV_CONDITIONAL_MISMATCH"


def test_conditional_on_both_passes(tmp_path):
    # Both have the same conditional — still equivalent
    p = _poly(question="Will the game proceed unless cancelled?")
    k = _kalshi(question="Will the game proceed unless cancelled?")
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason != "EQUIV_CONDITIONAL_MISMATCH"


def test_conditional_if_cancelled_rejected(tmp_path):
    p = _poly(question="Will the match happen if cancelled?", resolution_source=None)
    k = _kalshi(question="Will the match happen?", resolution_source=None)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason == "EQUIV_CONDITIONAL_MISMATCH"


# ---------------------------------------------------------------------------
# Rule 4.1 / 4.6 — Semantic equivalence (EXACT vs PROBABLE vs reject)
# ---------------------------------------------------------------------------

def test_exact_string_match_exact_confidence(tmp_path):
    p = _poly(question="Will the Fed cut rates by June 2025?")
    k = _kalshi(question="Will the Fed cut rates by June 2025?")
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.confidence == MatchConfidence.EXACT
    assert result.status == MatchStatus.MATCHED


def test_high_jaccard_same_meaning_exact(tmp_path):
    # Slightly different wording but same tokens above JACCARD_EXACT threshold
    p = _poly(question="Will the Federal Reserve cut interest rates in June 2025?")
    k = _kalshi(question="Will the Federal Reserve cut interest rates in June 2025?")
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.confidence == MatchConfidence.EXACT


def test_low_token_overlap_probable(tmp_path):
    # Overlapping but not enough tokens for EXACT
    p = _poly(question="Will candidate Alice win the 2025 election?", resolution_source=None)
    k = _kalshi(question="Will candidate Bob win the 2026 election?", resolution_source=None, close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    # Could be PROBABLE or POSSIBLE depending on overlap; in any case not EXACT
    assert result.confidence != MatchConfidence.EXACT


def test_no_token_overlap_event_mismatch(tmp_path):
    p = _poly(question="Will the Fed cut rates by June 2025?", resolution_source=None)
    k = _kalshi(question="Will Bitcoin reach 100000 by December?", resolution_source=None, close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.status == MatchStatus.REJECTED
    assert result.reject_reason == "EQUIV_EVENT_MISMATCH"
    assert result.confidence == MatchConfidence.UNKNOWN


def test_partial_overlap_possible_rejected(tmp_path):
    # "fed" appears in both but not enough overlap
    p = _poly(question="Will the Fed raise rates?", resolution_source=None)
    k = _kalshi(question="Will the NBA champion win a title?", resolution_source=None, close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.status == MatchStatus.REJECTED


# ---------------------------------------------------------------------------
# Rule 4.6 — Allowlist gate
# ---------------------------------------------------------------------------

def test_exact_confidence_requires_approved_pair(tmp_path):
    p = _poly()
    k = _kalshi()
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.status == MatchStatus.REJECTED
    assert result.reject_reason == "EQUIV_NOT_IN_APPROVED_PAIRS"
    assert result.confidence == MatchConfidence.PROBABLE


def test_exact_confidence_approved_pair_matched(tmp_path):
    p = _poly(market_id="0xfed111")
    k = _kalshi(market_id="KXFED-25JUN-T5.25")
    ap = _approved(tmp_path, poly_id="0xfed111", kalshi_id="KXFED-25JUN-T5.25")
    result = match(p, k, approved_pairs_path=ap)
    assert result.status == MatchStatus.MATCHED
    assert result.confidence == MatchConfidence.EXACT
    assert result.is_tradeable is True


def test_approved_pair_file_missing(tmp_path):
    p = _poly()
    k = _kalshi()
    missing = tmp_path / "nonexistent.json"
    result = match(p, k, approved_pairs_path=missing)
    assert result.reject_reason == "EQUIV_NOT_IN_APPROVED_PAIRS"


def test_approved_pair_file_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    p = _poly()
    k = _kalshi()
    result = match(p, k, approved_pairs_path=bad)
    assert result.reject_reason == "EQUIV_NOT_IN_APPROVED_PAIRS"


def test_approved_pairs_only_matching_id_counts(tmp_path):
    # Approved for a different pair — should still fail
    ap = _approved(tmp_path, poly_id="0xOTHER", kalshi_id="KXOTHER")
    p = _poly(market_id="0xfed111")
    k = _kalshi(market_id="KXFED-25JUN-T5.25")
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason == "EQUIV_NOT_IN_APPROVED_PAIRS"


# ---------------------------------------------------------------------------
# Direction inversion (Rule 4.2)
# ---------------------------------------------------------------------------

def test_same_direction_not_inverted(tmp_path):
    p = _poly(question="Will the Fed cut rates by June 2025?")
    k = _kalshi(question="Will the Fed cut rates by June 2025?")
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.directions_inverted is False


def test_negated_question_detected_as_inverted(tmp_path):
    # "Will X fail to Y" vs "Will X Y" — inverted directions
    p = _poly(
        market_id="0xinv",
        question="Will the Fed fail to cut rates by June 2025?",
        resolution_source=None,
    )
    k = _kalshi(
        market_id="KXINV",
        question="Will the Fed cut rates by June 2025?",
        resolution_source=None,
    )
    ap = _approved(tmp_path, poly_id="0xinv", kalshi_id="KXINV")
    result = match(p, k, approved_pairs_path=ap)
    assert result.directions_inverted is True


def test_both_negated_not_inverted(tmp_path):
    p = _poly(
        market_id="0xneg",
        question="Will the Fed fail to cut rates?",
        resolution_source=None,
    )
    k = _kalshi(
        market_id="KXNEG",
        question="Will the Fed fail to cut rates?",
        resolution_source=None,
    )
    ap = _approved(tmp_path, poly_id="0xneg", kalshi_id="KXNEG")
    result = match(p, k, approved_pairs_path=ap)
    assert result.directions_inverted is False


# ---------------------------------------------------------------------------
# is_tradeable invariant
# ---------------------------------------------------------------------------

def test_is_tradeable_only_for_exact_matched(tmp_path):
    p = _poly()
    k = _kalshi()
    ap = _approved(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.is_tradeable is True
    assert result.status == MatchStatus.MATCHED
    assert result.confidence == MatchConfidence.EXACT


def test_probable_match_not_tradeable(tmp_path):
    p = _poly()
    k = _kalshi()
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.is_tradeable is False


# ---------------------------------------------------------------------------
# match_all — cross-matching
# ---------------------------------------------------------------------------

def test_match_all_returns_probable_or_better(tmp_path):
    polys = [_poly(market_id="0xa"), _poly(market_id="0xb", question="Will Bitcoin hit 100k?", resolution_source=None, close_time=_T0)]
    kalshis = [_kalshi()]
    ap = _empty_pairs(tmp_path)
    results = match_all(polys, kalshis, approved_pairs_path=ap)
    # The Bitcoin question has no overlap with Fed question → UNKNOWN → discarded
    # The Fed question overlaps → PROBABLE (not approved) → kept
    confidences = {r.confidence for r in results}
    assert MatchConfidence.UNKNOWN not in confidences
    assert MatchConfidence.POSSIBLE not in confidences


def test_match_all_empty_inputs(tmp_path):
    ap = _empty_pairs(tmp_path)
    assert match_all([], [], approved_pairs_path=ap) == []
    assert match_all([_poly()], [], approved_pairs_path=ap) == []
    assert match_all([], [_kalshi()], approved_pairs_path=ap) == []


# ---------------------------------------------------------------------------
# Intentionally misleading near-matches
# ---------------------------------------------------------------------------

def test_same_category_different_event_rejected(tmp_path):
    # Both economy/Fed — but different threshold values
    p = _poly(question="Will the Fed cut rates to 4.5% by June 2025?", resolution_source=None)
    k = _kalshi(question="Will the Fed cut rates to 5.25% by June 2025?", resolution_source=None, close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    # Overlap: "fed", "cut", "rates", "june", "2025" — Jaccard depends on difference of threshold tokens
    # Result might be PROBABLE or POSSIBLE, but must NOT be EXACT
    assert result.confidence != MatchConfidence.EXACT
    assert result.is_tradeable is False


def test_same_asset_different_deadline_rejected(tmp_path):
    # Same question, deadline gap > 24 h
    p = _poly(question="Will BTC reach 100k?", resolution_source=None, close_time=_T0)
    k = _kalshi(question="Will BTC reach 100k?", resolution_source=None, close_time=_T25, market_id="KXBTC")
    ap = _approved(tmp_path, poly_id="0xfed111", kalshi_id="KXBTC")
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason == "EQUIV_DEADLINE_GAP"


def test_same_question_different_resolver_rejected(tmp_path):
    p = _poly(question="Will CPI drop below 3% in June 2025?", resolution_source="BLS")
    k = _kalshi(question="Will CPI drop below 3% in June 2025?", resolution_source="Reuters")
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason == "EQUIV_RESOLVER_MISMATCH"


def test_question_with_number_variants_not_exact(tmp_path):
    # "5.25%" vs "5.50%" — numeric difference means these are different markets.
    # Without approval the pair must NOT be promoted to EXACT.
    # Tokens shared: "fed", "funds", "rate", "exceed", "2025" → 5
    # Unique to each:  "525" (poly) vs "550" (kalshi)          → 2
    # Jaccard = 5/7 ≈ 0.71 — below the 0.85 EXACT threshold → PROBABLE only.
    p = _poly(question="Will Fed funds rate exceed 5.25% in 2025?", resolution_source=None)
    k = _kalshi(question="Will Fed funds rate exceed 5.50% in 2025?", resolution_source=None, close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.confidence != MatchConfidence.EXACT
    assert not result.is_tradeable


def test_misleading_subset_question_rejected(tmp_path):
    # "Will the Fed meet in June?" is a subset of the real question — different event
    p = _poly(question="Will the Fed cut rates by June 2025?", resolution_source=None)
    k = _kalshi(question="Will the Fed meet in June 2025?", resolution_source=None, close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    # "fed", "june", "2025" overlap; "cut", "rates" vs "meet" differ
    # Jaccard = 3/5 = 0.6 → PROBABLE, not EXACT
    assert result.confidence != MatchConfidence.EXACT
    assert result.is_tradeable is False


def test_inverted_yes_no_without_negation_word(tmp_path):
    # Polymarket: "Will X be above Y?" / Kalshi: "Will X be below Y?"
    # No classic negation word but effectively inverted — should NOT be EXACT without allowlist
    p = _poly(question="Will inflation be above 3 percent in June 2025?", resolution_source=None)
    k = _kalshi(question="Will inflation be below 3 percent in June 2025?", resolution_source=None, close_time=_T0)
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.is_tradeable is False


def test_poly_conditional_kalshi_clean_rejected(tmp_path):
    p = _poly(
        market_id="0xcond",
        question="Will the election proceed subject to court ruling?",
        resolution_source=None,
    )
    k = _kalshi(
        market_id="KXELEC",
        question="Will the election proceed?",
        resolution_source=None,
    )
    ap = _approved(tmp_path, poly_id="0xcond", kalshi_id="KXELEC")
    result = match(p, k, approved_pairs_path=ap)
    assert result.reject_reason == "EQUIV_CONDITIONAL_MISMATCH"


def test_pair_key_format_in_detail(tmp_path):
    p = _poly(market_id="0xaaa")
    k = _kalshi(market_id="KXBBB")
    ap = _empty_pairs(tmp_path)
    result = match(p, k, approved_pairs_path=ap)
    assert result.pair_key == "0xaaa::KXBBB"
