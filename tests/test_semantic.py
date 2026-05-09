"""
Unit tests for src/semantic.py.

Covers:
  - _normalize_entity: suffix preservation, middle-initial removal
  - _surnames: suffix-aware last-token extraction
  - _extract_event_type: new and existing family routing
  - semantics_compatible: family-level gating for new families
"""

from __future__ import annotations

from src.semantic import (
    MarketSemantics,
    _normalize_entity,
    _surnames,
    extract_semantics,
    semantics_compatible,
)


# ---------------------------------------------------------------------------
# _normalize_entity — suffix preservation
# ---------------------------------------------------------------------------

def test_normalize_entity_preserves_jr():
    assert _normalize_entity("Donald Trump Jr.") == "donald trump jr"


def test_normalize_entity_preserves_sr():
    assert _normalize_entity("John Smith Sr.") == "john smith sr"


def test_normalize_entity_preserves_ii():
    assert _normalize_entity("Robert Kennedy II") == "robert kennedy ii"


def test_normalize_entity_preserves_iii():
    assert _normalize_entity("Robert Kennedy III") == "robert kennedy iii"


def test_normalize_entity_preserves_iv():
    assert _normalize_entity("Henry IV") == "henry iv"


def test_normalize_entity_removes_middle_initial_keeps_jr():
    assert _normalize_entity("Donald J. Trump Jr.") == "donald trump jr"


def test_normalize_entity_removes_middle_initial_no_suffix():
    assert _normalize_entity("John F. Kennedy") == "john kennedy"


def test_normalize_entity_plain_name():
    assert _normalize_entity("Donald Trump") == "donald trump"


def test_normalize_entity_jr_not_collapsed_into_base():
    assert _normalize_entity("Donald Trump Jr.") != _normalize_entity("Donald Trump")


def test_normalize_entity_strips_trailing_period():
    assert _normalize_entity("Biden.") == "biden"


def test_normalize_entity_lowercases():
    assert _normalize_entity("HARRIS") == "harris"


# ---------------------------------------------------------------------------
# _surnames — suffix-aware extraction
# ---------------------------------------------------------------------------

def test_surnames_skips_jr():
    assert _surnames(frozenset({"donald trump jr"})) == frozenset({"trump"})


def test_surnames_skips_sr():
    assert _surnames(frozenset({"john smith sr"})) == frozenset({"smith"})


def test_surnames_skips_ii():
    assert _surnames(frozenset({"robert kennedy ii"})) == frozenset({"kennedy"})


def test_surnames_skips_iii():
    assert _surnames(frozenset({"robert kennedy iii"})) == frozenset({"kennedy"})


def test_surnames_skips_iv():
    assert _surnames(frozenset({"henry iv"})) == frozenset({"henry"})


def test_surnames_no_suffix():
    assert _surnames(frozenset({"donald trump"})) == frozenset({"trump"})


def test_surnames_multiple_entities():
    result = _surnames(frozenset({"donald trump jr", "joe biden"}))
    assert result == frozenset({"trump", "biden"})


def test_surnames_trump_jr_and_trump_share_surname():
    jr = _surnames(frozenset({"donald trump jr"}))
    base = _surnames(frozenset({"donald trump"}))
    assert jr == base == frozenset({"trump"})


def test_surnames_empty():
    assert _surnames(frozenset()) == frozenset()


def test_surnames_empty_string_ignored():
    assert _surnames(frozenset({""})) == frozenset()


# ---------------------------------------------------------------------------
# Event family routing — new families
# ---------------------------------------------------------------------------

def test_family_first_to_declare():
    sem = extract_semantics("Will Harris be the first candidate to announce?")
    assert sem.event_type == "first_to_declare"


def test_family_first_to_declare_variant():
    sem = extract_semantics("Who will be the first to declare a run?")
    assert sem.event_type == "first_to_declare"


def test_family_endorsement():
    sem = extract_semantics("Will Biden endorse Harris for president?")
    assert sem.event_type == "endorsement"


def test_family_endorsement_noun():
    sem = extract_semantics("Will Harris receive an endorsement from Obama?")
    assert sem.event_type == "endorsement"


def test_family_declare_run():
    sem = extract_semantics("Will DeSantis announce his run for president?")
    assert sem.event_type == "declare_run"


def test_family_declare_run_candidacy():
    sem = extract_semantics("Will Trump declare his candidacy before January?")
    assert sem.event_type == "declare_run"


def test_family_election_participation_run_for():
    sem = extract_semantics("Will DeSantis run for re-election as governor?")
    assert sem.event_type == "election_participation"


def test_family_election_participation_seek():
    sem = extract_semantics("Will Biden seek re-election in 2028?")
    assert sem.event_type == "election_participation"


def test_family_resignation_removal_resign():
    sem = extract_semantics("Will the Secretary of State resign?")
    assert sem.event_type == "resignation_removal"


def test_family_resignation_removal_step_down():
    sem = extract_semantics("Will the Speaker step down before the vote?")
    assert sem.event_type == "resignation_removal"


def test_family_resignation_removal_fired():
    sem = extract_semantics("Will the advisor be fired this week?")
    assert sem.event_type == "resignation_removal"


def test_family_leave_office_impeach():
    sem = extract_semantics("Will the president be impeached?")
    assert sem.event_type == "leave_office"


def test_family_leave_office_generic():
    sem = extract_semantics("Will the president leave office early?")
    assert sem.event_type == "leave_office"


def test_family_succession():
    sem = extract_semantics("Who will be Biden's successor as president?")
    assert sem.event_type == "succession"


def test_family_succession_noun():
    sem = extract_semantics("Will there be an orderly succession of power?")
    assert sem.event_type == "succession"


# ---------------------------------------------------------------------------
# Existing family routing — not regressed
# ---------------------------------------------------------------------------

def test_family_nomination_unchanged():
    sem = extract_semantics("Will Harris win the Democratic nomination?")
    assert sem.event_type == "nomination"


def test_family_general_election_win_unchanged():
    sem = extract_semantics("Will Trump win the 2024 election?")
    assert sem.event_type == "general_election_win"


def test_family_next_leader_unchanged():
    sem = extract_semantics("Will Harris become the next president?")
    assert sem.event_type == "next_leader"


def test_family_ticket_unchanged():
    sem = extract_semantics("Who will be on the Republican ticket?")
    assert sem.event_type == "ticket"


def test_family_vp_nomination_unchanged():
    sem = extract_semantics("Will Walz become vice president?")
    assert sem.event_type == "vp_nomination"


# ---------------------------------------------------------------------------
# semantics_compatible — new family checks
# ---------------------------------------------------------------------------

def _sem(event_type: str, **kwargs) -> MarketSemantics:
    defaults = dict(
        entities=frozenset(),
        cardinality=0,
        party=None,
        office=None,
        jurisdiction=None,
        departure_method=None,
    )
    defaults.update(kwargs)
    return MarketSemantics(event_type=event_type, **defaults)


def test_compat_resignation_removal_same_family():
    a = _sem("resignation_removal")
    b = _sem("resignation_removal")
    ok, _ = semantics_compatible(a, b)
    assert ok


def test_compat_resignation_removal_vs_leave_office_rejected():
    a = _sem("resignation_removal")
    b = _sem("leave_office")
    ok, detail = semantics_compatible(a, b)
    assert not ok
    assert "event_family" in detail


def test_compat_succession_office_mismatch_rejected():
    a = _sem("succession", office="president")
    b = _sem("succession", office="senator")
    ok, detail = semantics_compatible(a, b)
    assert not ok
    assert "office" in detail


def test_compat_endorsement_same_entity_passes():
    a = _sem("endorsement", entities=frozenset({"joe biden"}), cardinality=1)
    b = _sem("endorsement", entities=frozenset({"joe biden"}), cardinality=1)
    ok, _ = semantics_compatible(a, b)
    assert ok


def test_compat_declare_run_party_mismatch_rejected():
    a = _sem("declare_run", party="republican")
    b = _sem("declare_run", party="democratic")
    ok, detail = semantics_compatible(a, b)
    assert not ok
    assert "party" in detail


def test_compat_election_participation_office_mismatch_rejected():
    a = _sem("election_participation", office="president")
    b = _sem("election_participation", office="senator")
    ok, detail = semantics_compatible(a, b)
    assert not ok
    assert "office" in detail


def test_compat_first_to_declare_party_mismatch_rejected():
    a = _sem("first_to_declare", party="republican")
    b = _sem("first_to_declare", party="democratic")
    ok, detail = semantics_compatible(a, b)
    assert not ok
    assert "party" in detail
