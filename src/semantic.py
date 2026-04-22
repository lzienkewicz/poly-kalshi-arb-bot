"""
Structural semantic extraction for market equivalence checking.

Extracts three fields from a market question:
  entities    — frozen set of normalized person/entity names
  event_type  — canonical category: nomination, election, ticket,
                appointment, departure, legislation, other
  cardinality — number of distinct entities (1 = single, 2 = pair)

Two markets are structurally compatible iff:
  cardinality matches (when both sides have entities), AND
  entity surnames match (when both sides have entities), AND
  event_type is non-"other" on both sides and matches.

Surname comparison ("Trump" == "Donald Trump") allows partial-name
questions from different venues to match without false negatives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSemantics:
    entities: frozenset[str]   # normalized lowercase full names
    event_type: str            # canonical type or "other"
    cardinality: int           # number of entities extracted


# ---------------------------------------------------------------------------
# Words that are capitalized in market questions but are NOT person names.
# Used to filter out false positives from title-case extraction.
# ---------------------------------------------------------------------------

_NON_ENTITY_WORDS: frozenset[str] = frozenset({
    # Party / affiliation
    "republican", "democrat", "democratic", "gop", "independent",
    "progressive", "conservative", "liberal", "libertarian",
    # Titles / roles
    "president", "senator", "governor", "secretary", "speaker",
    "vice", "prime", "minister", "representative", "congressman",
    "congresswoman", "attorney", "general", "justice", "mayor",
    "chancellor", "premier", "director", "administrator", "ambassador",
    # Political bodies / generic geographic
    "united", "states", "america", "american", "white", "house",
    "congress", "senate", "supreme", "court", "federal", "national",
    "new", "north", "south", "east", "west", "central",
    # Question / grammar words (sometimes capitalized in titles)
    "will", "who", "what", "which", "when", "does", "is", "are",
    "the", "a", "an", "and", "or", "but", "for",
    # Event-type nouns — not person names
    "nomination", "nominee", "election", "ticket", "primary",
    "presidency", "presidential", "candidate", "running", "vote",
    "inauguration", "administration", "cabinet", "coalition",
    "runoff", "reelection",
    # Temporal
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
})


def _is_non_entity(word: str) -> bool:
    return word.lower().rstrip(".,!?") in _NON_ENTITY_WORDS


# ---------------------------------------------------------------------------
# Entity extraction — two pattern approaches, fallback to nothing
# ---------------------------------------------------------------------------

# Pattern 1: "Will [Name+] [and [Name+]] ..."
_WILL_RE = re.compile(
    r"[Ww]ill\s+"
    r"("
    r"[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*"
    r"(?:\s+and\s+[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*)?)"
    r"\s"
)

# Pattern 2: "[Name+] [and [Name+]] wins/becomes/is nominated/..." at sentence start
_LEAD_RE = re.compile(
    r"^([A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*"
    r"(?:\s+and\s+[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*)?)"
    r"\s+(?:win|wins|beat|beats|become|becomes|is\s+(?:the\s+)?(?:nominee|nominated|elected)"
    r"|will\s+(?:win|be|become)|gets?\s+(?:the\s+)?(?:nomination|elected)|named|elected)\b",
    re.IGNORECASE,
)

_SUFFIX_RE = re.compile(r"\s+(jr|sr|iii|ii|iv)\s*$", re.IGNORECASE)


def _normalize_entity(name: str) -> str:
    """Lowercase, strip punctuation dots, remove honorific suffixes."""
    name = name.strip().lower()
    name = name.replace(".", "")
    name = _SUFFIX_RE.sub("", name)
    return name.strip()


def _split_subject(subject: str) -> list[str]:
    """Split 'X [and Y]' into filtered entity strings."""
    parts = re.split(r"\s+and\s+", subject, flags=re.IGNORECASE)
    result = []
    for part in parts:
        words = part.strip().split()
        entity_words = [w for w in words if not _is_non_entity(w)]
        if entity_words:
            result.append(" ".join(entity_words))
    return result


def _extract_entities(question: str) -> list[str]:
    """Return list of raw entity strings from the market question."""
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
    """Return the last token of each entity (surname) for flexible comparison.

    "donald trump" → {"trump"}, "trump" → {"trump"} — so they match.
    """
    return frozenset(e.split()[-1] for e in entities if e)


# ---------------------------------------------------------------------------
# Event type extraction — priority-ordered (first match wins)
# ---------------------------------------------------------------------------

_EVENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Ticket must come before nomination (ticket questions often contain "presidential")
    (re.compile(r"\bticket\b", re.IGNORECASE), "ticket"),
    (re.compile(r"\brunning.?mate\b", re.IGNORECASE), "ticket"),
    # VP nomination — before general nomination to avoid misclassification
    (re.compile(r"\bvice[\s-]?(president|presidency)\b", re.IGNORECASE), "vp_nomination"),
    # Presidential / general nomination
    (re.compile(r"\bnominat(e|ed|ion|ing|or)\b|\bnominee\b", re.IGNORECASE), "nomination"),
    (re.compile(r"\bprimary\b|\bprimaries\b", re.IGNORECASE), "nomination"),
    # These synonyms canonicalize to "nomination"
    (re.compile(r"\bwin\s+(the\s+)?\w+\s*nomination\b", re.IGNORECASE), "nomination"),
    (re.compile(r"\bbe\s+(the\s+)?\w+\s*nominee\b", re.IGNORECASE), "nomination"),
    # General-election win
    (re.compile(
        r"\bwin\s+(the\s+)?(?:\w+\s+)?election\b"
        r"|\belected\s+(president|senator|governor|mayor|congress)\b",
        re.IGNORECASE,
    ), "election"),
    (re.compile(
        r"\b(become|be)\s+(the\s+)?(next\s+)?(president|senator|governor|mayor)\b",
        re.IGNORECASE,
    ), "election"),
    (re.compile(r"\bwins?\s+(the\s+)?(?:general|presidential)\s+election\b", re.IGNORECASE), "election"),
    # Departure from office
    (re.compile(
        r"\b(resign(s|ed|ation)?|step(s|ped)?\s+down|leave\s+office|remov(e|ed|al))\b",
        re.IGNORECASE,
    ), "departure"),
    (re.compile(r"\bimpeach(ed|ment)?\b", re.IGNORECASE), "departure"),
    # Appointment / Senate confirmation
    (re.compile(r"\b(appoint(ed|ment)?|confirm(ed|ation)?)\b", re.IGNORECASE), "appointment"),
    # Legislation
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
    return MarketSemantics(
        entities=entities,
        event_type=_extract_event_type(question),
        cardinality=len(raw_entities),
    )


def semantics_compatible(a: MarketSemantics, b: MarketSemantics) -> tuple[bool, str]:
    """Return (compatible, detail_string).

    Skips a check when either side lacks extraction data — unusual question
    formats shouldn't block valid pairs, and the Jaccard layer provides a
    second filter for those cases.

    Entity comparison uses surnames only so "Donald Trump" and "Trump" match,
    but "Trump" and "Trump and Rubio" still fail on cardinality.
    """
    # Cardinality: check whenever both sides produced entities
    if a.cardinality > 0 and b.cardinality > 0:
        if a.cardinality != b.cardinality:
            return False, f"cardinality mismatch: {a.cardinality} vs {b.cardinality}"

    # Entity surnames: check whenever both sides produced entities
    if a.entities and b.entities:
        a_sur = _surnames(a.entities)
        b_sur = _surnames(b.entities)
        if a_sur != b_sur:
            return False, (
                f"entity mismatch: {sorted(a_sur)} vs {sorted(b_sur)}"
            )

    # Event type: check whenever both sides resolved to a non-generic type
    if a.event_type != "other" and b.event_type != "other":
        if a.event_type != b.event_type:
            return False, f"event_type mismatch: {a.event_type!r} vs {b.event_type!r}"

    return True, ""
