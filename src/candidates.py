"""
Candidate-pair generation for cross-venue arb matching.

Narrows both venue pools to a single domain, excludes combo markets, pairs only
markets whose trading-close deadlines fall within a configurable window, and
drops pairs below a minimum Jaccard threshold before returning.

Pipeline stages:
  fetched        all active markets from adapters
  domain         matching MATCH_DOMAIN
  simple         single-event only (combos excluded)
  time_window    close_time gap <= DEADLINE_WINDOW
  jaccard_floor  Jaccard >= JACCARD_MIN (pre-matcher garbage elimination)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import structlog

from src.models.market import Market
from src.normalization import normalize_market

log = structlog.get_logger(__name__)

MatchDomain = Literal["sports", "crypto", "politics", "entertainment", "all"]

_DEFAULT_DEADLINE_WINDOW = timedelta(hours=48)
_JACCARD_MIN = 0.15   # pairs below this are noise; skip before matcher sees them


# ---------------------------------------------------------------------------
# Domain classification  (tight rules to avoid cross-domain pollution)
# ---------------------------------------------------------------------------

# Explicit category strings returned by each venue API — most reliable signal
_DOMAIN_CATS: dict[str, frozenset[str]] = {
    "sports": frozenset({
        "sports",
        # US leagues
        "nba", "nfl", "nhl", "mlb", "ncaa", "pga",
        "college football", "college basketball",
        # global
        "soccer", "football", "basketball", "baseball",
        "hockey", "tennis", "golf", "mma", "ufc",
        "boxing", "racing", "motorsports", "esports",
        # Kalshi sometimes uses these
        "american football", "ice hockey",
    }),
    "crypto": frozenset({
        "crypto", "cryptocurrency", "bitcoin", "ethereum", "defi", "web3",
    }),
    "politics": frozenset({
        "politics", "political", "elections", "election", "government",
        "us politics", "world politics", "geopolitics",
    }),
    "entertainment": frozenset({
        "entertainment", "movies", "music", "tv", "television",
        "awards", "pop culture", "film",
    }),
}

# Strong sports signals in the question text — must be unambiguous
_SPORTS_QUESTION_RES: list[re.Pattern] = [
    # matchup format  "Lakers vs Celtics", "Team A v Team B"
    re.compile(r"\bvs\.?\s+\w|\bv\.\s+\w", re.IGNORECASE),
    # betting totals  "over 218.5 points", "under 4.5 goals"
    re.compile(
        r"\b(over|under)\s+\d+(\.\d+)?\s*(points?|goals?|runs?|rebounds?|assists?|yards?|kills?)",
        re.IGNORECASE,
    ),
    # player prop threshold  "30+ points", "2+ goals"
    re.compile(r"\d+\+\s*(points?|goals?|runs?|rebounds?|assists?|yards?)", re.IGNORECASE),
    # major sports events
    re.compile(
        r"\b(stanley cup|super bowl|world series|nba finals?|nfl playoffs?|"
        r"mlb playoffs?|nhl playoffs?|champions league|world cup|olympics?|"
        r"ncaa tournament|march madness)\b",
        re.IGNORECASE,
    ),
    # league abbreviations as whole words
    re.compile(r"\b(nba|nfl|nhl|mlb|ufc|mma|pga|wnba|cfl|afl)\b", re.IGNORECASE),
]

_CRYPTO_RE = re.compile(
    r"\b(btc|eth|bitcoin|ethereum|solana|sol|doge|xrp|crypto|binance|coinbase)\b",
    re.IGNORECASE,
)
_POLITICS_RE = re.compile(
    r"\b(president|senator|governor|congress|congressman|congresswoman|"
    r"election|electoral|parliament|prime minister|chancellor|mayor|"
    r"democrat|republican|conservative|labour|liberal|ballot|vote|votes?)\b",
    re.IGNORECASE,
)


def classify_domain(market: Market) -> str:
    """Return one of: sports / crypto / politics / entertainment / other.

    Category field is checked first (most reliable).  Question-text patterns
    are a fallback and use tight regexes to avoid cross-domain pollution.
    """
    cat = (market.category or "").lower().strip()
    q = market.question

    # Category match — authoritative
    for domain, cats in _DOMAIN_CATS.items():
        if cat in cats:
            return domain

    # Question-text fallback — tight patterns only
    for pat in _SPORTS_QUESTION_RES:
        if pat.search(q):
            return "sports"

    if _CRYPTO_RE.search(q):
        return "crypto"

    if _POLITICS_RE.search(q):
        return "politics"

    return "other"


# ---------------------------------------------------------------------------
# Combo / multi-condition detection
#
# Goal: exclude markets that bundle multiple independent conditions into one
# contract (same-game parlays, multi-prop chains).  Do NOT exclude simple
# single-prop markets that happen to start with "Yes" or contain one comma.
# ---------------------------------------------------------------------------

_COMBO_RE: list[re.Pattern] = [
    # Two separate "Yes" clauses  e.g. "Yes Edwards: 20+ | Yes Over 218.5"
    # Requires at least 10 chars between the two "yes" tokens.
    re.compile(r"\byes\b.{10,}\byes\b", re.IGNORECASE),
    # "both A and B"
    re.compile(r"\bboth\b.{3,50}\band\b", re.IGNORECASE),
    # "2 of the following", "all of the following", "each of"
    re.compile(r"\b\d+\s*\+?\s*of\s+(the\s+)?following\b", re.IGNORECASE),
    re.compile(r"\ball\s+of\s+the\s+following\b", re.IGNORECASE),
    re.compile(r"\beach\s+of\b", re.IGNORECASE),
    # Explicit multi-leg connector
    re.compile(r"\band\b.{10,}\band\b.{10,}\band\b", re.IGNORECASE),  # A and B and C
]


def is_combo_market(market: Market) -> bool:
    """Return True if the question bundles multiple independent conditions.

    Conservative: only triggers on clear multi-condition signals so that
    simple Kalshi single-prop markets (e.g. "Yes Anthony Edwards: 20+ pts")
    are not incorrectly excluded.
    """
    q = market.question
    # Many commas with repeated "yes" style conditions
    if q.lower().count("yes") >= 2 and q.count(",") >= 2:
        return True
    for pat in _COMBO_RE:
        if pat.search(q):
            return True
    return False


# ---------------------------------------------------------------------------
# Candidate pair + stage counts
# ---------------------------------------------------------------------------

@dataclass
class CandidatePair:
    poly: Market
    kalshi: Market
    jaccard: float
    poly_days: float    # close_time days from now
    kalshi_days: float


@dataclass
class StageCount:
    poly_fetched: int = 0
    kalshi_fetched: int = 0
    poly_domain: int = 0
    kalshi_domain: int = 0
    poly_simple: int = 0
    kalshi_simple: int = 0
    time_window_pairs: int = 0
    time_skipped: int = 0
    jaccard_floor_dropped: int = 0
    final_candidates: int = 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_candidates(
    poly_markets: list[Market],
    kalshi_markets: list[Market],
    *,
    domain: MatchDomain = "sports",
    deadline_window: timedelta = _DEFAULT_DEADLINE_WINDOW,
    jaccard_min: float = _JACCARD_MIN,
    now: datetime,
) -> tuple[list[CandidatePair], StageCount]:
    """Filter and pair markets, returning candidates sorted by Jaccard desc.

    Deadline comparison uses close_time on both sides.  For sports Polymarket
    markets close_time == game_start_time, which aligns with Kalshi's trading-
    close time for the same event.  Using resolution_time would shift Polymarket
    deadlines days past Kalshi's window and yield zero pairs.
    """
    counts = StageCount(
        poly_fetched=len(poly_markets),
        kalshi_fetched=len(kalshi_markets),
    )

    # Stage 3: domain filter
    if domain != "all":
        poly_d = [m for m in poly_markets if classify_domain(m) == domain]
        kalshi_d = [m for m in kalshi_markets if classify_domain(m) == domain]
    else:
        poly_d = list(poly_markets)
        kalshi_d = list(kalshi_markets)
    counts.poly_domain = len(poly_d)
    counts.kalshi_domain = len(kalshi_d)

    # Stage 4: simple-market filter
    poly_s = [m for m in poly_d if not is_combo_market(m)]
    kalshi_s = [m for m in kalshi_d if not is_combo_market(m)]
    counts.poly_simple = len(poly_s)
    counts.kalshi_simple = len(kalshi_s)

    if not poly_s or not kalshi_s:
        log.info(
            "candidates_empty_after_filter",
            domain=domain,
            poly_simple=counts.poly_simple,
            kalshi_simple=counts.kalshi_simple,
        )
        return [], counts

    # Precompute canonical forms once
    poly_canon = [(normalize_market(m), m) for m in poly_s]
    kalshi_canon = [(normalize_market(m), m) for m in kalshi_s]

    # Stage 5: time-window cross-filter
    # Stage 6: Jaccard floor — drop noise before matcher
    pairs: list[CandidatePair] = []
    time_skipped = 0
    jaccard_dropped = 0

    for cp, poly in poly_canon:
        poly_deadline = cp.close_time
        poly_days = (poly_deadline - now).total_seconds() / 86400
        for ck, kalshi in kalshi_canon:
            kalshi_deadline = ck.close_time
            if abs(poly_deadline - kalshi_deadline) > deadline_window:
                time_skipped += 1
                continue
            j = _jaccard(cp.question_tokens, ck.question_tokens)
            if j < jaccard_min:
                jaccard_dropped += 1
                continue
            kalshi_days = (kalshi_deadline - now).total_seconds() / 86400
            pairs.append(CandidatePair(
                poly=poly,
                kalshi=kalshi,
                jaccard=j,
                poly_days=poly_days,
                kalshi_days=kalshi_days,
            ))

    counts.time_skipped = time_skipped
    counts.time_window_pairs = time_skipped + len(pairs) + jaccard_dropped - time_skipped
    counts.jaccard_floor_dropped = jaccard_dropped
    counts.final_candidates = len(pairs)
    pairs.sort(key=lambda p: p.jaccard, reverse=True)

    log.info(
        "candidates_generated",
        domain=domain,
        poly_simple=counts.poly_simple,
        kalshi_simple=counts.kalshi_simple,
        time_window_pairs=counts.time_window_pairs,
        jaccard_dropped=jaccard_dropped,
        final_candidates=counts.final_candidates,
    )
    return pairs, counts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
