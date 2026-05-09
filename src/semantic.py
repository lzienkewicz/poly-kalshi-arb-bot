"""
Structural semantic extraction for market equivalence checking.

Each market question is routed to exactly one event family:

  nomination           — party primary / nomination contest
  ticket               — running-mate / joint ticket market
  vp_nomination        — vice-presidential nomination specifically
  first_to_declare     — "Who will be first to announce their candidacy?"
  endorsement          — "Will X endorse Y?"
  declare_run          — "Will X announce/declare a run?"
  election_participation — "Will X run for / appear on the ballot?"
  general_election_win — "Will X win the [Y] election?"
  next_leader          — "Will X be the next [office]?" (succession / parliamentary)
  leave_office         — impeachment, generic "leave office"
  resignation_removal  — explicit resignation, firing, or removal from office
  succession           — "Who will succeed X as Y?"
  appointment          — cabinet / judicial confirmation
  legislation          — bills, vetoes, law passage
  other                — everything else

Within each family, compatibility is checked on:
  cardinality          — 1-entity vs 2-entity → reject
  entity surnames      — "Donald Trump" ≈ "Trump" (last non-suffix token)
  event_type           — families must match exactly
  party                — (nomination/declare_run) republican vs democratic → reject
  office               — (election / next_leader / succession) president vs senator → reject
  jurisdiction         — (next_leader / succession) us vs uk → reject
  departure_method     — (leave_office / resignation_removal) resign vs impeach → reject

Checks are only applied when BOTH sides have the relevant data — missing
data on one side is treated as "unknown / compatible", since extraction
can fail for unusual question formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSemantics:
    entities: frozenset[str]          # normalized lowercase full names
    event_type: str                   # event family string (see module docstring)
    cardinality: int                  # number of entities extracted
    party: str | None = None          # republican | democratic | None
    office: str | None = None         # president | senator | governor | prime_minister | …
    jurisdiction: str | None = None   # us | uk | france | … | None
    departure_method: str | None = None  # resign | impeach | remove | None


# ---------------------------------------------------------------------------
# Words that are capitalized in questions but are NOT person names
# ---------------------------------------------------------------------------

_NON_ENTITY_WORDS: frozenset[str] = frozenset({
    # Party / affiliation
    "republican", "democrat", "democratic", "gop", "independent",
    "progressive", "conservative", "liberal", "libertarian",
    # Titles / roles (as standalone words — not surnames)
    "president", "senator", "governor", "secretary", "speaker",
    "vice", "prime", "minister", "representative", "congressman",
    "congresswoman", "attorney", "general", "justice", "mayor",
    "chancellor", "premier", "director", "administrator", "ambassador",
    # Political bodies / generic geographic words
    "united", "states", "america", "american", "white", "house",
    "congress", "senate", "supreme", "court", "federal", "national",
    "new", "north", "south", "east", "west", "central",
    # Grammar words (sometimes Title-Cased in question titles)
    "will", "who", "what", "which", "when", "does", "is", "are",
    "the", "a", "an", "and", "or", "but", "for",
    # Event-type nouns — not person names
    "nomination", "nominee", "election", "ticket", "primary",
    "presidency", "presidential", "candidate", "running", "vote",
    "inauguration", "administration", "cabinet", "coalition",
    "runoff", "reelection", "general",
    # Temporal
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
})


def _is_non_entity(word: str) -> bool:
    return word.lower().rstrip(".,!?") in _NON_ENTITY_WORDS


# ---------------------------------------------------------------------------
# Entity extraction — "Will X [and Y] …" and leading-subject pattern
# ---------------------------------------------------------------------------

_WILL_RE = re.compile(
    r"[Ww]ill\s+"
    r"("
    r"[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*"
    r"(?:\s+and\s+[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*)?)"
    r"\s"
)

_LEAD_RE = re.compile(
    r"^([A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*"
    r"(?:\s+and\s+[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*)?)"
    r"\s+(?:win|wins|beat|beats|become|becomes|"
    r"is\s+(?:the\s+)?(?:nominee|nominated|elected)|"
    r"will\s+(?:win|be|become)|gets?\s+(?:the\s+)?(?:nomination|elected)|"
    r"named|elected)\b",
    re.IGNORECASE,
)

_SUFFIX_WORDS: frozenset[str] = frozenset({"jr", "sr", "ii", "iii", "iv"})


def _normalize_entity(name: str) -> str:
    """Normalize a person name for entity comparison.

    Removes periods and middle initials (single-char tokens) but preserves
    generational suffixes so "Donald Trump Jr." != "Donald Trump".

    "Donald Trump Jr."    -> "donald trump jr"
    "Donald J. Trump Jr." -> "donald trump jr"
    "Donald Trump"        -> "donald trump"
    """
    name = name.strip().lower().replace(".", "")
    tokens = [t for t in name.split() if len(t) > 1 or t in _SUFFIX_WORDS]
    return " ".join(tokens)


def _split_subject(subject: str) -> list[str]:
    parts = re.split(r"\s+and\s+", subject, flags=re.IGNORECASE)
    result = []
    for part in parts:
        words = part.strip().split()
        entity_words = [w for w in words if not _is_non_entity(w)]
        if entity_words:
            result.append(" ".join(entity_words))
    return result


def _extract_entities(question: str) -> list[str]:
    m = _WILL_RE.search(question)
    if m:
        entities = _split_subject(m.group(1))
        if entities:
            return entities
    m = _LEAD_RE.match(question)
    if m:
        entities = _split_subject(m.group(1))
        if entities:
            return entities
    return []


def _surnames(entities: frozenset[str]) -> frozenset[str]:
    """Last *non-suffix* token of each entity — "Donald Trump Jr." ≈ "Trump"."""
    result: set[str] = set()
    for e in entities:
        if not e:
            continue
        for tok in reversed(e.split()):
            if tok not in _SUFFIX_WORDS:
                result.add(tok)
                break
    return frozenset(result)


# ---------------------------------------------------------------------------
# Family-specific attribute extractors
# ---------------------------------------------------------------------------

_PARTY_RE = re.compile(r"\b(republican|democratic|democrat|gop)\b", re.IGNORECASE)
_PARTY_MAP: dict[str, str] = {
    "republican": "republican",
    "democratic": "democratic",
    "democrat": "democratic",
    "gop": "republican",
}


def _extract_party(question: str) -> str | None:
    m = _PARTY_RE.search(question)
    return _PARTY_MAP.get(m.group(1).lower()) if m else None


_OFFICE_RE = re.compile(
    r"\b(president(?:ial)?|prime\s+minister|pm\b|chancellor|governor|senator|"
    r"senate\b|mayor|speaker|congressman|congresswoman|representative|"
    r"vice\s+president)\b",
    re.IGNORECASE,
)
_OFFICE_MAP: dict[str, str] = {
    "president": "president",
    "presidential": "president",
    "prime minister": "prime_minister",
    "pm": "prime_minister",
    "chancellor": "chancellor",
    "governor": "governor",
    "senator": "senator",
    "senate": "senator",
    "mayor": "mayor",
    "speaker": "speaker",
    "congressman": "representative",
    "congresswoman": "representative",
    "representative": "representative",
    "vice president": "vice_president",
}


def _extract_office(question: str) -> str | None:
    m = _OFFICE_RE.search(question)
    if m:
        key = m.group(1).lower()
        return _OFFICE_MAP.get(key) or _OFFICE_MAP.get(key.replace("  ", " "))
    return None


_JURISDICTION_RE = re.compile(
    r"\b(us|u\.s\.|american|united\s+states|uk|u\.k\.|british|united\s+kingdom|"
    r"french|france|german|germany|canadian|canada|australian|australia)\b",
    re.IGNORECASE,
)
_JURISDICTION_MAP: dict[str, str] = {
    "us": "us", "u.s.": "us", "american": "us", "united states": "us",
    "uk": "uk", "u.k.": "uk", "british": "uk", "united kingdom": "uk",
    "french": "france", "france": "france",
    "german": "germany", "germany": "germany",
    "canadian": "canada", "canada": "canada",
    "australian": "australia", "australia": "australia",
}


def _extract_jurisdiction(question: str) -> str | None:
    m = _JURISDICTION_RE.search(question)
    return _JURISDICTION_MAP.get(m.group(1).lower()) if m else None


# Departure method: specific mechanism (resign / impeach / remove).
# "leave office" alone is too generic — maps to None so it's compatible with all.
_DEPARTURE_METHOD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bimpeach(ed|ment)?\b", re.IGNORECASE), "impeach"),
    (re.compile(r"\bresign(s|ed|ation)?\b", re.IGNORECASE), "resign"),
    (re.compile(r"\bstep(s|ped)?\s+down\b", re.IGNORECASE), "resign"),
    (re.compile(r"\b(fired|dismissed|ousted)\b", re.IGNORECASE), "remove"),
    (re.compile(r"\b(removed|removal)\s+from\s+office\b", re.IGNORECASE), "remove"),
]


def _extract_departure_method(question: str) -> str | None:
    for pat, method in _DEPARTURE_METHOD_PATTERNS:
        if pat.search(question):
            return method
    return None


# ---------------------------------------------------------------------------
# Event family routing — priority order (first match wins)
# ---------------------------------------------------------------------------

_EVENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ── ticket (before nomination — ticket questions often say "presidential") ──
    (re.compile(r"\bticket\b", re.IGNORECASE), "ticket"),
    (re.compile(r"\brunning.?mate\b", re.IGNORECASE), "ticket"),
    # ── VP nomination (before general nomination) ──────────────────────────
    (re.compile(r"\bvice[\s-]?(president|presidency)\b", re.IGNORECASE), "vp_nomination"),
    # ── first_to_declare (before endorsement and nomination) ───────────────
    (re.compile(r"\bfirst\s+(candidate\s+)?to\s+(announce|declare)\b", re.IGNORECASE), "first_to_declare"),
    (re.compile(r"\bfirst\s+(major\s+)?(candidate|politician)\s+to\s+(announce|declare)\b", re.IGNORECASE), "first_to_declare"),
    # ── endorsement (before nomination — "endorse the nominee" must not → nomination) ─
    (re.compile(r"\bendors(e|es|ed|ing|ement)\b", re.IGNORECASE), "endorsement"),
    # ── declare_run (before nomination) ────────────────────────────────────
    (re.compile(
        r"\b(announce|declare|launch)\s+(their|a|his|her)\s+(run|campaign|candidacy|bid)\b",
        re.IGNORECASE,
    ), "declare_run"),
    (re.compile(r"\benter\s+(the\s+)?(race|primary|contest)\b", re.IGNORECASE), "declare_run"),
    # ── nomination ────────────────────────────────────────────────────────
    (re.compile(r"\bnominat(e|ed|ion|ing|or)\b|\bnominee\b", re.IGNORECASE), "nomination"),
    (re.compile(r"\bprimary\b|\bprimaries\b", re.IGNORECASE), "nomination"),
    (re.compile(r"\bwin\s+(the\s+)?\w+\s*nomination\b", re.IGNORECASE), "nomination"),
    (re.compile(r"\bbe\s+(the\s+)?\w+\s*nominee\b", re.IGNORECASE), "nomination"),
    # ── resignation_removal (specific departure — before generic leave_office) ─
    (re.compile(r"\b(resign(s|ed|ation)?|step(s|ped)?\s+down)\b", re.IGNORECASE), "resignation_removal"),
    (re.compile(r"\b(fired|dismissed|ousted)\b", re.IGNORECASE), "resignation_removal"),
    (re.compile(r"\b(removed|removal)\s+from\s+office\b", re.IGNORECASE), "resignation_removal"),
    # ── leave_office (impeachment + generic "leave office" phrase) ─────────
    (re.compile(r"\bimpeach(ed|ment)?\b", re.IGNORECASE), "leave_office"),
    (re.compile(r"\bleave\s+office\b", re.IGNORECASE), "leave_office"),
    # ── election_participation (run for / appear on ballot) ────────────────
    (re.compile(
        r"\brun\s+for\s+(re-?election|president|senator|governor|mayor|congress\w*|office)\b",
        re.IGNORECASE,
    ), "election_participation"),
    (re.compile(r"\bseek(s|ing)?\s+(re-?election|a\s+second\s+term|another\s+term)\b", re.IGNORECASE), "election_participation"),
    (re.compile(r"\b(appear|qualify)\s+on\s+(the\s+)?\w+\s+ballot\b", re.IGNORECASE), "election_participation"),
    # ── succession (before next_leader) ────────────────────────────────────
    (re.compile(r"\b(successor|succession)\b", re.IGNORECASE), "succession"),
    # ── general_election_win (explicit election-win language) ─────────────
    # Must come BEFORE next_leader so "be elected president" → general_election_win
    (re.compile(r"\bwin\s+(the\s+)?\w*\s*election\b", re.IGNORECASE), "general_election_win"),
    (re.compile(
        r"\belected\s+(president|senator|governor|mayor|congress\w*)\b",
        re.IGNORECASE,
    ), "general_election_win"),
    (re.compile(r"\bwins?\s+(the\s+)?presidency\b", re.IGNORECASE), "general_election_win"),
    (re.compile(r"\belection\s+winner\b", re.IGNORECASE), "general_election_win"),
    # ── next_leader (office succession / parliamentary framing) ───────────
    # Allow one optional adjective between "next" and the office name so that
    # "be the next French president" and "be the next UK prime minister" route
    # here correctly. general_election_win is checked first, so "be elected
    # president" is already captured before we reach this block.
    (re.compile(
        r"\bbe\s+(the\s+)?(next\s+)?(?:\w+\s+)?(president|prime\s+minister|chancellor"
        r"|governor|senator|mayor|speaker)\b",
        re.IGNORECASE,
    ), "next_leader"),
    (re.compile(
        r"\bbecome\s+(the\s+)?(next\s+)?(?:\w+\s+)?(president|prime\s+minister|chancellor"
        r"|governor|senator|mayor|speaker)\b",
        re.IGNORECASE,
    ), "next_leader"),
    (re.compile(
        r"\bnext\s+(?:\w+\s+)?(president|prime\s+minister|uk\s+pm|us\s+president|chancellor)\b",
        re.IGNORECASE,
    ), "next_leader"),
    # ── appointment ───────────────────────────────────────────────────────
    (re.compile(r"\b(appoint(ed|ment)?|confirm(ed|ation)?)\b", re.IGNORECASE), "appointment"),
    # ── legislation ───────────────────────────────────────────────────────
    (re.compile(
        r"\b(pass(ed)?|signed?\s+into\s+law|veto(ed)?|enacted?|become\s+law)\b",
        re.IGNORECASE,
    ), "legislation"),
]


def _extract_event_type(question: str) -> str:
    for pattern, event_type in _EVENT_PATTERNS:
        if pattern.search(question):
            return event_type
    return "other"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_semantics(question: str) -> MarketSemantics:
    """Extract structured semantic fields from a market question string."""
    raw_entities = _extract_entities(question)
    entities = frozenset(_normalize_entity(e) for e in raw_entities if e)
    event_type = _extract_event_type(question)
    return MarketSemantics(
        entities=entities,
        event_type=event_type,
        cardinality=len(raw_entities),
        party=_extract_party(question),
        office=_extract_office(question),
        jurisdiction=_extract_jurisdiction(question),
        departure_method=_extract_departure_method(question) if event_type in ("leave_office", "resignation_removal") else None,
    )


def semantics_compatible(a: MarketSemantics, b: MarketSemantics) -> tuple[bool, str]:
    """Return (compatible, detail_string).

    Returns False with a reason string when the pair should be hard-rejected.
    Returns True when the pair is structurally plausible (still subject to
    Jaccard and matcher scoring downstream).

    Checks are skipped when either side lacks the relevant data — extraction
    failures on unusual question formats should not cause false negatives.
    """
    # ── Event family must match ────────────────────────────────────────────
    if a.event_type != "other" and b.event_type != "other":
        if a.event_type != b.event_type:
            return False, f"event_family mismatch: {a.event_type!r} vs {b.event_type!r}"

    family = a.event_type if a.event_type != "other" else b.event_type

    # ── Cardinality ───────────────────────────────────────────────────────
    if a.cardinality > 0 and b.cardinality > 0:
        if a.cardinality != b.cardinality:
            return False, f"cardinality mismatch: {a.cardinality} vs {b.cardinality}"

    # ── Entity surnames ───────────────────────────────────────────────────
    if a.entities and b.entities:
        a_sur = _surnames(a.entities)
        b_sur = _surnames(b.entities)
        if a_sur != b_sur:
            return False, f"entity mismatch: {sorted(a_sur)} vs {sorted(b_sur)}"

    # ── Family-specific attribute checks ──────────────────────────────────
    if family in ("nomination", "vp_nomination", "ticket", "declare_run",
                  "first_to_declare", "election_participation"):
        if a.party and b.party and a.party != b.party:
            return False, f"party mismatch: {a.party!r} vs {b.party!r}"
        if family in ("election_participation", "declare_run"):
            if a.office and b.office and a.office != b.office:
                return False, f"office mismatch: {a.office!r} vs {b.office!r}"

    elif family in ("general_election_win", "next_leader", "succession"):
        if a.office and b.office and a.office != b.office:
            return False, f"office mismatch: {a.office!r} vs {b.office!r}"
        if a.jurisdiction and b.jurisdiction and a.jurisdiction != b.jurisdiction:
            return False, f"jurisdiction mismatch: {a.jurisdiction!r} vs {b.jurisdiction!r}"

    elif family in ("leave_office", "resignation_removal"):
        # resign vs impeach are mutually exclusive; absence of method is compatible with either
        if a.departure_method and b.departure_method and a.departure_method != b.departure_method:
            return False, (
                f"departure_method mismatch: {a.departure_method!r} vs {b.departure_method!r}"
            )

    return True, ""
