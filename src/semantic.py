"""
Structural semantic extraction for market equivalence checking.

Extracts three fields from a market question:
  entities    — frozen set of normalized person/entity names
  event_type  — canonical category: nomination, election, ticket,
                appointment, departure, legislation, other
  cardinality — number of distinct entities (1 = single, 2 = pair)

Two markets are structurally compatible iff:
  entities are non-empty on both sides and match, AND
  event_type is non-"other" on both sides and matches, AND
  cardinality matches (when entities are available).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSemantics:
    entities: frozenset[str]   # normalized lowercase names
    event_type: str            # canonical type or "other"
    cardinality: int           # number of entities extracted


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

# Matches "Will [Entity1 [and Entity2]] <verb>..."
# Entity words are Title-Case (start with [A-Z]) — stops when lowercase word found.
_WILL_SUBJECT_RE = re.compile(
    r"[Ww]ill\s+"
    r"("
    r"[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*"
    r"(?:\s+and\s+[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*)?)"
    r"\s"
)

_SUFFIX_RE = re.compile(r"\s+(jr|sr|iii|ii|iv)\s*$", re.IGNORECASE)


def _normalize_entity(name: str) -> str:
    """Lowercase, remove punctuation dots, strip honorific suffixes."""
    name = name.strip().lower()
    name = name.replace(".", "")       # "j.d." → "jd", "jr." → "jr"
    name = _SUFFIX_RE.sub("", name)    # remove Jr/Sr/III/etc.
    return name.strip()


def _extract_entities(question: str) -> list[str]:
    """Return list of raw entity names from 'Will X [and Y] ...' pattern."""
    m = _WILL_SUBJECT_RE.search(question)
    if not m:
        return []
    subject = m.group(1)
    parts = re.split(r"\s+and\s+", subject, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Event type extraction — priority-ordered (first match wins)
# ---------------------------------------------------------------------------

_EVENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Ticket must come before nomination (ticket questions often say "presidential")
    (re.compile(r"\bticket\b", re.IGNORECASE), "ticket"),
    (re.compile(r"\brunning.?mate\b", re.IGNORECASE), "ticket"),
    # VP nomination — must come before general nomination to avoid misclassification
    (re.compile(r"\bvice[\s-]?(president|presidency)\b", re.IGNORECASE), "vp_nomination"),
    # Presidential / general nomination
    (re.compile(r"\bnominat(e|ed|ion|ing|or)\b|\bnominee\b", re.IGNORECASE), "nomination"),
    (re.compile(r"\bprimary\b|\bprimaries\b", re.IGNORECASE), "nomination"),
    # General-election win
    (re.compile(r"\bwin\s+(the\s+)?election\b|\belected\s+(president|senator|governor|mayor|congress)\b", re.IGNORECASE), "election"),
    (re.compile(r"\b(become|be)\s+(the\s+)?(next\s+)?(president|senator|governor|mayor)\b", re.IGNORECASE), "election"),
    # Departure from office
    (re.compile(r"\b(resign(s|ed|ation)?|step(s|ped)?\s+down|leave\s+office|remov(e|ed|al))\b", re.IGNORECASE), "departure"),
    (re.compile(r"\bimpeach(ed|ment)?\b", re.IGNORECASE), "departure"),
    # Appointment / Senate confirmation
    (re.compile(r"\b(appoint(ed|ment)?|confirm(ed|ation)?)\b", re.IGNORECASE), "appointment"),
    # Legislation
    (re.compile(r"\b(pass(ed)?|signed?\s+into\s+law|veto(ed)?|enacted?|become\s+law)\b", re.IGNORECASE), "legislation"),
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

    Skips a check when either side lacks data — extraction may fail for
    unusual question formats, and we prefer not to block valid pairs.
    """
    if a.entities and b.entities:
        if a.entities != b.entities:
            return False, f"entity mismatch: {sorted(a.entities)} vs {sorted(b.entities)}"
        if a.cardinality != b.cardinality:
            return False, f"cardinality mismatch: {a.cardinality} vs {b.cardinality}"

    if a.event_type != "other" and b.event_type != "other":
        if a.event_type != b.event_type:
            return False, f"event_type mismatch: {a.event_type!r} vs {b.event_type!r}"

    return True, ""
