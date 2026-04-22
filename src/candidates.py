"""
Candidate-pair generation for cross-venue arb matching.

Pipeline stages:
  fetched            all active markets from adapters
  domain             matching MATCH_DOMAIN
  sports_subdomain   championship_futures / match_winner / spread_total / player_prop / all
  simple             single-event only (combos excluded)
  time_window        close_time gap <= DEADLINE_WINDOW
  jaccard_floor      Jaccard >= JACCARD_MIN
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import structlog

from src.models.market import Market
from src.normalization import normalize_market

log = structlog.get_logger(__name__)

MatchDomain = Literal["sports", "crypto", "politics", "entertainment", "all"]
SportsSubdomain = Literal[
    "championship_futures", "award_futures", "match_winner", "spread_total", "player_prop", "all"
]

_DEFAULT_DEADLINE_WINDOW = timedelta(hours=48)
_JACCARD_MIN = 0.15


# ---------------------------------------------------------------------------
# Domain classification
# ---------------------------------------------------------------------------

_DOMAIN_CATS: dict[str, frozenset[str]] = {
    "sports": frozenset({
        "sports", "nba", "nfl", "nhl", "mlb", "ncaa", "pga",
        "college football", "college basketball",
        "soccer", "football", "basketball", "baseball",
        "hockey", "tennis", "golf", "mma", "ufc",
        "boxing", "racing", "motorsports", "esports",
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

_SPORTS_QUESTION_RES: list[re.Pattern] = [
    re.compile(r"\bvs\.?\s+\w|\bv\.\s+\w", re.IGNORECASE),
    re.compile(
        r"\b(over|under)\s+\d+(\.\d+)?\s*(points?|goals?|runs?|rebounds?|assists?|yards?|kills?)",
        re.IGNORECASE,
    ),
    re.compile(r"\d+\+\s*(points?|goals?|runs?|rebounds?|assists?|yards?)", re.IGNORECASE),
    re.compile(
        r"\b(stanley cup|super bowl|world series|nba finals?|nfl playoffs?|"
        r"mlb playoffs?|nhl playoffs?|champions league|world cup|olympics?|"
        r"ncaa tournament|march madness)\b",
        re.IGNORECASE,
    ),
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
    """Return: sports / crypto / politics / entertainment / other."""
    cat = (market.category or "").lower().strip()
    q = market.question

    for domain, cats in _DOMAIN_CATS.items():
        if cat in cats:
            return domain

    for pat in _SPORTS_QUESTION_RES:
        if pat.search(q):
            return "sports"

    if _CRYPTO_RE.search(q):
        return "crypto"
    if _POLITICS_RE.search(q):
        return "politics"

    return "other"


# ---------------------------------------------------------------------------
# Sports subdomain classification
# ---------------------------------------------------------------------------

# Named championships / tournaments — must appear in the question
_CHAMP_NAMES_RE = re.compile(
    r"\b("
    # Hockey
    r"stanley cup|nhl (finals?|championship|playoffs?|title)|"
    # Basketball
    r"nba (finals?|championship|title|playoffs?)|"
    r"ncaa (tournament|championship|title)|march madness|"
    r"eastern conference (finals?|championship)|western conference (finals?|championship)|"
    # Football
    r"super bowl|nfl (championship|title|playoffs?)|"
    # Baseball
    r"world series|mlb (championship|title|playoffs?)|"
    # Soccer
    r"world cup|champions league|europa league|premier league title|"
    r"la liga title|bundesliga title|serie a title|"
    # Tennis Grand Slams
    r"wimbledon|us open|french open|australian open|"
    # Golf majors
    r"the masters|pga championship|the open championship|u\.?s\.? open|"
    # Other
    r"grey cup|memorial cup|wnba (finals?|championship|title)|"
    r"mls cup|copa america|euro \d{4}|euros \d{4}"
    r")\b",
    re.IGNORECASE,
)

_WIN_RE = re.compile(
    r"\b(win|wins|winner|champion|champions|championship|title|trophy|lift)\b",
    re.IGNORECASE,
)

# Individual sports awards — "Will X win the NBA MVP / Hart Trophy / Rookie of the Year?"
_AWARD_FUTURES_RE = re.compile(
    r"\b("
    # NBA awards
    r"nba mvp|nba (most valuable player)|"
    r"rookie of the year|roy\b|"
    r"sixth man of the year|"
    r"most improved player|"
    r"defensive player of the year|dpoy\b|"
    r"clutch player of the year|"
    r"coach of the year|executive of the year|"
    r"all[-\s]?nba|all[-\s]?star mvp|finals mvp|"
    # NHL trophies
    r"hart (memorial )?trophy|vezina trophy|norris trophy|calder (memorial )?trophy|"
    r"conn smythe|art ross trophy|rocket richard trophy|"
    r"lady byng|selke trophy|jack adams|"
    # NFL / MLB / other
    r"heisman|cy young|mvp award|league mvp|"
    # Draft picks
    r"first( overall)? pick|#?1 (overall )?pick|first pick of the \w+ draft|"
    r"\d+(st|nd|rd|th) pick|top pick"
    r")\b",
    re.IGNORECASE,
)

# Match-winner: explicit head-to-head matchup
_MATCH_RE = re.compile(r"\bvs\.?\s|\bv\.\s|\bbeat\b|\bdefeat\b|\bface\b", re.IGNORECASE)

# Spread / total markets
_SPREAD_TOTAL_RE = re.compile(
    r"\b(over|under|spread|total|more than|fewer than|at least|at most)\s+\d",
    re.IGNORECASE,
)

# Player prop: player name + stat threshold (crude but effective heuristic)
_PLAYER_PROP_RE = re.compile(
    r"\d+\+\s*(points?|goals?|assists?|rebounds?|hits?|runs?|strikeout|yards?|tackles?)|"
    r"(points?|goals?|assists?|rebounds?|hits?|runs?|strikeout|yards?)\s*:\s*\d+",
    re.IGNORECASE,
)


def classify_sports_subdomain(market: Market) -> str:
    """Return: championship_futures / match_winner / spread_total / player_prop / other_sports.

    Checked in priority order — a market matching championship signals is classified
    as championship_futures even if it also contains "vs".
    """
    q = market.question

    if _CHAMP_NAMES_RE.search(q) and _WIN_RE.search(q):
        return "championship_futures"

    if _AWARD_FUTURES_RE.search(q) and _WIN_RE.search(q):
        return "award_futures"

    if _PLAYER_PROP_RE.search(q):
        return "player_prop"

    if _SPREAD_TOTAL_RE.search(q):
        return "spread_total"

    if _MATCH_RE.search(q):
        return "match_winner"

    return "other_sports"


# ---------------------------------------------------------------------------
# Combo / multi-condition detection
# ---------------------------------------------------------------------------

_COMBO_RE: list[re.Pattern] = [
    re.compile(r"\byes\b.{10,}\byes\b", re.IGNORECASE),
    re.compile(r"\bboth\b.{3,50}\band\b", re.IGNORECASE),
    re.compile(r"\b\d+\s*\+?\s*of\s+(the\s+)?following\b", re.IGNORECASE),
    re.compile(r"\ball\s+of\s+the\s+following\b", re.IGNORECASE),
    re.compile(r"\beach\s+of\b", re.IGNORECASE),
    re.compile(r"\band\b.{10,}\band\b.{10,}\band\b", re.IGNORECASE),
]


def is_combo_market(market: Market) -> bool:
    """Return True if the question bundles multiple independent conditions."""
    q = market.question
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
    poly_days: float
    kalshi_days: float


@dataclass
class StageCount:
    poly_fetched: int = 0
    kalshi_fetched: int = 0
    poly_domain: int = 0
    kalshi_domain: int = 0
    poly_subdomain: int = 0
    kalshi_subdomain: int = 0
    poly_simple: int = 0
    kalshi_simple: int = 0
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
    sports_subdomain: SportsSubdomain = "championship_futures",
    deadline_window: timedelta = _DEFAULT_DEADLINE_WINDOW,
    jaccard_min: float = _JACCARD_MIN,
    now: datetime,
) -> tuple[list[CandidatePair], StageCount]:
    """Filter and pair markets, returning candidates sorted by Jaccard desc."""
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

    # Stage 3.5: sports subdomain filter
    if domain == "sports" and sports_subdomain != "all":
        poly_d = [m for m in poly_d if classify_sports_subdomain(m) == sports_subdomain]
        kalshi_d = [m for m in kalshi_d if classify_sports_subdomain(m) == sports_subdomain]
    counts.poly_subdomain = len(poly_d)
    counts.kalshi_subdomain = len(kalshi_d)

    # Stage 4: simple-market filter
    poly_s = [m for m in poly_d if not is_combo_market(m)]
    kalshi_s = [m for m in kalshi_d if not is_combo_market(m)]
    counts.poly_simple = len(poly_s)
    counts.kalshi_simple = len(kalshi_s)

    if not poly_s or not kalshi_s:
        log.info(
            "candidates_empty_after_filter",
            domain=domain,
            sports_subdomain=sports_subdomain,
            poly_simple=counts.poly_simple,
            kalshi_simple=counts.kalshi_simple,
        )
        return [], counts

    poly_canon = [(normalize_market(m), m) for m in poly_s]
    kalshi_canon = [(normalize_market(m), m) for m in kalshi_s]

    # Stage 5: time window + Stage 6: Jaccard floor
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
    counts.jaccard_floor_dropped = jaccard_dropped
    counts.final_candidates = len(pairs)
    pairs.sort(key=lambda p: p.jaccard, reverse=True)

    log.info(
        "candidates_generated",
        domain=domain,
        sports_subdomain=sports_subdomain,
        poly_simple=counts.poly_simple,
        kalshi_simple=counts.kalshi_simple,
        time_skipped=time_skipped,
        jaccard_dropped=jaccard_dropped,
        final_candidates=counts.final_candidates,
    )
    return pairs, counts


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
