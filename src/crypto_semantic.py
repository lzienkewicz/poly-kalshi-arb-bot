"""
Crypto-domain semantic extraction and compatibility checking.

Event families
--------------
touch_target_by_date    — "Will BTC hit $100k by December 2025?"
                          Intraday touch/reach of a threshold (comparator="reach").
above_below_on_date     — "Will BTC close above $80k on Dec 31?" or
                          "Will BTC be above $80k on Jan 1?"
                          Endpoint price (closing or at-date) relative to threshold.
                          Comparators: close_above, close_below, above, below,
                                       at_least, at_most
ETF_approval            — "Will a spot Bitcoin ETF be approved by the SEC?"
protocol_event          — "Will the Bitcoin halving occur before April 2024?"
other                   — anything else

Key distinction:
  "hit" / "reach" → intraday touching of a price level → touch_target_by_date
  "close above" / "above" / "below" → endpoint price    → above_below_on_date
  These two families are hard-rejected against each other even if asset,
  threshold, and deadline all match.

Comparator normalization:
  hit / reach / touch / surpass / cross  → "reach"       (touch_target_by_date)
  above / over / exceeds                 → "above"       (above_below_on_date)
  below / under / less than              → "below"       (above_below_on_date)
  close above / closes above             → "close_above" (above_below_on_date)
  close below / closes below             → "close_below" (above_below_on_date)
  at least / no less than                → "at_least"    (above_below_on_date)
  at most / no more than                 → "at_most"     (above_below_on_date)

"hit 100k" and "reach 100k" are synonyms (both → comparator="reach").
"close above 100k" and "above 100k" route to the same family (above_below_on_date)
but differ on comparator and will be hard-rejected against each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class CryptoSemantics:
    event_family: str              # see module docstring
    asset: str | None              # normalized symbol: BTC, ETH, SOL, …
    threshold: float | None        # normalized USD price value
    comparator: str | None         # reach | above | below | close_above | close_below | at_least | at_most
    deadline: str | None           # "YYYY-MM" | "YYYY" | None
    approval_target: str | None    # spot | futures | staking | options | None
    jurisdiction: str | None       # us | eu | uk | None
    has_conditional: bool = False  # True when question has an "if …" condition


# ---------------------------------------------------------------------------
# Asset normalization — name/ticker → canonical ticker
# ---------------------------------------------------------------------------

_ASSET_SYMBOLS: dict[str, str] = {
    # Bitcoin
    "bitcoin": "BTC", "btc": "BTC",
    # Ethereum
    "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
    # Solana
    "solana": "SOL", "sol": "SOL",
    # Dogecoin
    "dogecoin": "DOGE", "doge": "DOGE",
    # XRP / Ripple
    "xrp": "XRP", "ripple": "XRP",
    # Cardano
    "cardano": "ADA", "ada": "ADA",
    # Binance Coin
    "binance coin": "BNB", "bnb": "BNB",
    # Polkadot
    "polkadot": "DOT", "dot": "DOT",
    # Chainlink
    "chainlink": "LINK",
    # Avalanche
    "avalanche": "AVAX", "avax": "AVAX",
    # Litecoin
    "litecoin": "LTC", "ltc": "LTC",
    # Polygon / MATIC
    "polygon": "MATIC", "matic": "MATIC",
    # NEAR
    "near protocol": "NEAR", "near": "NEAR",
    # Tron
    "tron": "TRX", "trx": "TRX",
    # Cosmos
    "cosmos": "ATOM", "atom": "ATOM",
    # Arbitrum
    "arbitrum": "ARB", "arb": "ARB",
    # Optimism
    "optimism": "OP",
    # Uniswap
    "uniswap": "UNI", "uni": "UNI",
    # Shiba Inu
    "shiba inu": "SHIB", "shiba": "SHIB", "shib": "SHIB",
    # Sui
    "sui": "SUI",
    # Pepe
    "pepe": "PEPE",
}

# Sort longest-first so "near protocol" matches before "near"
_ASSET_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_ASSET_SYMBOLS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _extract_asset(question: str) -> str | None:
    m = _ASSET_RE.search(question)
    return _ASSET_SYMBOLS.get(m.group(1).lower()) if m else None


# ---------------------------------------------------------------------------
# Price threshold extraction and normalization
# ---------------------------------------------------------------------------

# "$100k", "$1.5M", "$100,000" — dollar-prefixed values.
# No \s* before suffix: prevents "$100,000 by" from grabbing "b" as billions.
_PRICE_DOLLAR_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)([kmb])?\b",
    re.IGNORECASE,
)
# "100k", "1.5M" — no dollar sign, requires k/m/b suffix immediately after the
# digits (no whitespace) to avoid matching bare integers like years or day counts.
_PRICE_SHORTHAND_RE = re.compile(
    r"\b([\d,]+(?:\.\d+)?)([kmb])\b",
    re.IGNORECASE,
)

_MULTIPLIERS: dict[str, float] = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_price(num_str: str, suffix: str) -> float | None:
    try:
        value = float(num_str.replace(",", "")) * _MULTIPLIERS.get(suffix.lower(), 1.0)
        return value if value > 0 else None
    except (ValueError, AttributeError):
        return None


def _extract_threshold(question: str) -> float | None:
    """Return the first plausible price threshold from the question.

    Dollar-prefixed values are preferred over bare shorthand (100k) to avoid
    accidentally matching "3 days" or similar non-price numbers.
    """
    for m in _PRICE_DOLLAR_RE.finditer(question):
        v = _parse_price(m.group(1), m.group(2) or "")
        if v is not None:
            return v
    for m in _PRICE_SHORTHAND_RE.finditer(question):
        v = _parse_price(m.group(1), m.group(2))
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Comparator extraction
# ---------------------------------------------------------------------------

# "close above/below" MUST be checked before plain "above/below"
_COMPARATOR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bclose[sd]?\s+(above|over|at)\b", re.IGNORECASE), "close_above"),
    (re.compile(r"\bclose[sd]?\s+(below|under)\b", re.IGNORECASE), "close_below"),
    (re.compile(r"\b(hit|reach|touch|surpass|cross(?:es)?)\b", re.IGNORECASE), "reach"),
    (re.compile(r"\b(above|over|exceed[s]?|greater\s+than)\b", re.IGNORECASE), "above"),
    (re.compile(r"\b(below|under|less\s+than|fall\s+below|drop\s+below)\b", re.IGNORECASE), "below"),
    (re.compile(r"\b(at\s+least|no\s+less\s+than|minimum)\b", re.IGNORECASE), "at_least"),
    (re.compile(r"\b(at\s+most|no\s+more\s+than|maximum)\b", re.IGNORECASE), "at_most"),
]


def _extract_comparator(question: str) -> str | None:
    for pattern, comp in _COMPARATOR_PATTERNS:
        if pattern.search(question):
            return comp
    return None


# ---------------------------------------------------------------------------
# Deadline extraction and normalization
# ---------------------------------------------------------------------------

_CURRENT_YEAR: int = datetime.now(timezone.utc).year

_MONTH_NUM: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# "by end of 2025", "end of 2025", "end of year 2025", "end of the year 2025"
_EOY_YEAR_RE = re.compile(
    r"\bend\s+of\s+(?:(?:the\s+)?year\s+)?(\d{4})\b",
    re.IGNORECASE,
)
# "by end of year", "by EOY", "end of the year"
_EOY_RE = re.compile(r"\bend\s+of\s+(?:the\s+)?year\b|\beoy\b", re.IGNORECASE)
# "Q3 2025", "Q4 of 2025"
_QUARTER_RE = re.compile(r"\b(q[1-4])\s*(?:of\s+)?(\d{4})\b", re.IGNORECASE)
# "December 31, 2025" or "Dec 31 2025" — month + day (+ optional year)
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+\d{1,2}(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
# Bare year: "in 2025", "by 2026", "before 2027"
_YEAR_RE = re.compile(r"\b(20[2-9]\d)\b")
# "December 2025" — month + bare year (no day)
_MONTH_YEAR_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+(20[2-9]\d)\b",
    re.IGNORECASE,
)
# Month-only (no day, no year following immediately) — used only as last resort
_MONTH_ONLY_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"(?!\.?\s+\d)\b",
    re.IGNORECASE,
)

_QUARTER_LAST_MONTH: dict[str, int] = {"q1": 3, "q2": 6, "q3": 9, "q4": 12}


def _extract_deadline(question: str) -> str | None:
    """Return canonical deadline string: "YYYY-MM", "YYYY", or None.

    Normalization:
      "by end of year" / "EOY"    → "{current_year}-12"
      "by end of 2025"            → "2025-12"
      "by Q4 2025"                → "2025-12"  (last month of the quarter)
      "by December 31, 2025"      → "2025-12"
      "by December 2025"          → "2025-12"
      "by December" (no year)     → "{current_year}-12"
      "by 2025" (year only)       → "2025"
    """
    # Specific year end
    m = _EOY_YEAR_RE.search(question)
    if m:
        return f"{m.group(1)}-12"

    # Generic "end of year" → current year
    if _EOY_RE.search(question):
        return f"{_CURRENT_YEAR}-12"

    # Quarter + year → last month of that quarter
    m = _QUARTER_RE.search(question)
    if m:
        q = m.group(1).lower()
        year = m.group(2)
        return f"{year}-{_QUARTER_LAST_MONTH[q]:02d}"

    # Month (+ optional day) + optional year
    m = _MONTH_DAY_YEAR_RE.search(question)
    if m:
        month_key = m.group(1)[:3].lower()
        month_num = _MONTH_NUM.get(month_key, 0)
        # Year may be captured in group 2, or fall back to a bare year elsewhere
        year_str = m.group(2)
        if not year_str:
            m_year = _YEAR_RE.search(question)
            year_str = m_year.group(1) if m_year else str(_CURRENT_YEAR)
        if month_num:
            return f"{year_str}-{month_num:02d}"

    # "December 2025" — month + year without a day
    m = _MONTH_YEAR_RE.search(question)
    if m:
        month_key = m.group(1)[:3].lower()
        month_num = _MONTH_NUM.get(month_key, 0)
        if month_num:
            return f"{m.group(2)}-{month_num:02d}"

    # Bare year
    m = _YEAR_RE.search(question)
    if m:
        return m.group(1)

    # Month-only (no year, no day)
    m = _MONTH_ONLY_RE.search(question)
    if m:
        month_key = m.group(1)[:3].lower()
        month_num = _MONTH_NUM.get(month_key, 0)
        if month_num:
            return f"{_CURRENT_YEAR}-{month_num:02d}"

    return None


def _deadlines_compatible(a: str | None, b: str | None) -> bool:
    """True when deadlines are considered equivalent for arb purposes.

    Rules:
      Both None             → compatible (no deadline info on either side)
      One None, one set     → compatible (partial info; don't reject on absence)
      Same string           → compatible
      Different year        → incompatible (hard reject)
      Same year, one lacks month precision → compatible (year-level match)
      Same year, both have month precision, months differ → incompatible
    """
    if a is None or b is None:
        return True
    if a == b:
        return True
    if a[:4] != b[:4]:
        return False  # different years
    # Same year — if either is year-only (len==4), accept
    if len(a) == 4 or len(b) == 4:
        return True
    # Both have sub-year precision and they differ
    return False


# ---------------------------------------------------------------------------
# ETF approval target
# ---------------------------------------------------------------------------

_ETF_TARGET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bspot\b", re.IGNORECASE), "spot"),
    (re.compile(r"\bfutures?\b", re.IGNORECASE), "futures"),
    (re.compile(r"\bstaking\b", re.IGNORECASE), "staking"),
    (re.compile(r"\boptions?\b", re.IGNORECASE), "options"),
]


def _extract_approval_target(question: str) -> str | None:
    for pat, target in _ETF_TARGET_PATTERNS:
        if pat.search(question):
            return target
    return None


# ---------------------------------------------------------------------------
# Jurisdiction (regulatory body / country)
# ---------------------------------------------------------------------------

_CRYPTO_JURIS_RE = re.compile(
    r"\b(sec\b|cftc\b|us\b|u\.s\.|american|united\s+states|"
    r"eu\b|european\s+union|european|uk\b|british|united\s+kingdom)\b",
    re.IGNORECASE,
)
_CRYPTO_JURIS_MAP: dict[str, str] = {
    "sec": "us", "cftc": "us", "us": "us", "u.s.": "us",
    "american": "us", "united states": "us",
    "eu": "eu", "european union": "eu", "european": "eu",
    "uk": "uk", "british": "uk", "united kingdom": "uk",
}


def _extract_jurisdiction(question: str) -> str | None:
    m = _CRYPTO_JURIS_RE.search(question)
    return _CRYPTO_JURIS_MAP.get(m.group(1).lower()) if m else None


# ---------------------------------------------------------------------------
# Conditional structure detection
# ---------------------------------------------------------------------------

_CONDITIONAL_SIGNALS: frozenset[str] = frozenset({
    "if ", "assuming ", "contingent ", "barring ", "unless ",
    "provided that ", "subject to ", "in the event ",
})


def _has_conditional(question: str) -> bool:
    q = question.lower()
    return any(sig in q for sig in _CONDITIONAL_SIGNALS)


# ---------------------------------------------------------------------------
# Protocol event detection
# ---------------------------------------------------------------------------

_PROTOCOL_EVENT_RE = re.compile(
    r"\b(halving|the\s+merge|merge\b|upgrade\b|hard\s+fork|mainnet|"
    r"shapella|cancun|dencun|pectra|beacon\s+chain|"
    r"proof.of.stake|proof.of.work|"
    r"eip[\s-]?\d+)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Event family routing (priority order)
# ---------------------------------------------------------------------------

_ETF_RE = re.compile(r"\betf\b|\bexchange.traded.fund\b", re.IGNORECASE)
_ETF_ACTION_RE = re.compile(
    r"\b(approv(?:ed?|al)|reject(?:ed)?|list(?:ed)?|launch(?:ed)?|"
    r"pass(?:ed)?|denied?|greenlit?|granted?|filed?|sec\s+approv)\b",
    re.IGNORECASE,
)
_PRICE_ACTION_RE = re.compile(
    r"\b(hit|reach|touch|surpass|cross(?:es)?|above|below|over|under|"
    r"at\s+least|at\s+most|exceed[s]?|greater\s+than|less\s+than|"
    r"trade[sd]?\s+at|worth|valued?\s+at)\b",
    re.IGNORECASE,
)


def _extract_event_family(question: str) -> str:
    """Route a crypto question to exactly one event family (priority order).

    Price markets are sub-routed by comparator:
      comparator=="reach"  → touch_target_by_date  (intraday touch)
      everything else      → above_below_on_date   (endpoint condition)
    """
    # ETF approval — checked first, before price checks
    if _ETF_RE.search(question) and _ETF_ACTION_RE.search(question):
        return "ETF_approval"

    # Price markets — require both a price action word AND an extractable threshold
    if _PRICE_ACTION_RE.search(question) and _extract_threshold(question) is not None:
        comp = _extract_comparator(question)
        if comp == "reach":
            return "touch_target_by_date"
        return "above_below_on_date"

    # Protocol events — no price threshold required
    if _PROTOCOL_EVENT_RE.search(question):
        return "protocol_event"

    return "other"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_crypto_semantics(question: str) -> CryptoSemantics:
    """Extract structured crypto-specific fields from a market question."""
    family = _extract_event_family(question)
    return CryptoSemantics(
        event_family=family,
        asset=_extract_asset(question),
        threshold=_extract_threshold(question),
        comparator=_extract_comparator(question),
        deadline=_extract_deadline(question),
        approval_target=_extract_approval_target(question) if family == "ETF_approval" else None,
        jurisdiction=_extract_jurisdiction(question),
        has_conditional=_has_conditional(question),
    )


def crypto_semantics_compatible(
    a: CryptoSemantics, b: CryptoSemantics
) -> tuple[bool, str]:
    """Return (compatible, detail_string) for two crypto market semantics.

    Hard reject rules (applied in order of discrimination power):
      1. Event family must match
      2. Conditional structure must match (conditional ≠ unconditional)
      3. Asset must match (when both specified)
      4. Comparator must match (for price-family markets)
      5. Threshold must match (when both specified)
      6. Deadline must be compatible (year-level or exact month)
      7. ETF approval target must match (spot ≠ futures)
      8. Jurisdiction must match (when both specified)
    """
    # ── Event family ──────────────────────────────────────────────────────
    if a.event_family != "other" and b.event_family != "other":
        if a.event_family != b.event_family:
            return False, f"event_family mismatch: {a.event_family!r} vs {b.event_family!r}"

    family = a.event_family if a.event_family != "other" else b.event_family

    # ── Conditional structure ─────────────────────────────────────────────
    if a.has_conditional != b.has_conditional:
        return False, "conditional mismatch: one side has a conditional clause, the other does not"

    # ── Asset ─────────────────────────────────────────────────────────────
    if a.asset and b.asset and a.asset != b.asset:
        return False, f"asset mismatch: {a.asset!r} vs {b.asset!r}"

    # ── Comparator (price families only) ─────────────────────────────────
    if family in ("touch_target_by_date", "above_below_on_date"):
        if a.comparator and b.comparator and a.comparator != b.comparator:
            return False, f"comparator mismatch: {a.comparator!r} vs {b.comparator!r}"

    # ── Threshold ─────────────────────────────────────────────────────────
    if a.threshold is not None and b.threshold is not None:
        if a.threshold != b.threshold:
            return False, (
                f"threshold mismatch: {a.threshold:,.0f} vs {b.threshold:,.0f}"
            )

    # ── Deadline ──────────────────────────────────────────────────────────
    if not _deadlines_compatible(a.deadline, b.deadline):
        return False, f"deadline mismatch: {a.deadline!r} vs {b.deadline!r}"

    # ── ETF approval target ───────────────────────────────────────────────
    if family == "ETF_approval":
        if a.approval_target and b.approval_target and a.approval_target != b.approval_target:
            return False, (
                f"approval_target mismatch: {a.approval_target!r} vs {b.approval_target!r}"
            )

    # ── Jurisdiction ──────────────────────────────────────────────────────
    if a.jurisdiction and b.jurisdiction and a.jurisdiction != b.jurisdiction:
        return False, f"jurisdiction mismatch: {a.jurisdiction!r} vs {b.jurisdiction!r}"

    return True, ""
