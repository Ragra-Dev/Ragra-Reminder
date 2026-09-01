from datetime import date

import pytest

from ragra.relevance.engine import RelevanceDecision, is_relevant
from ragra.relevance.profile import UserAcademicProfile, load_profile


def _profile(*, expected_semester: int = 3) -> UserAcademicProfile:
    return UserAcademicProfile(
        program="CS",
        expected_semester=expected_semester,
        enrolled_courses=["OOP Theory"],
        section_labels={"OOP Theory": "CS-C"},
        enrollment_config={},
    )


def test_matching_section_is_relevant():
    decision = is_relevant("Lab 02 Section C", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.RELEVANT


def test_other_section_is_suppressed():
    decision = is_relevant("Lab 02 Section D", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.OTHER_SECTION


def test_explicit_multi_section_mention_including_mine_is_relevant():
    decision = is_relevant("Section C & D", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.RELEVANT


def test_explicit_multi_section_mention_excluding_mine_is_other_section():
    decision = is_relevant("Section D & E", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.OTHER_SECTION


def test_unknown_course_is_relevant_by_default():
    # No profile data for this course at all - no evidence to suppress on.
    decision = is_relevant("Lab 02 Section D", "", "Some Other Course", _profile())
    assert decision == RelevanceDecision.RELEVANT


def test_all_sections_announcement_is_relevant():
    decision = is_relevant("Announcement for all sections", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.RELEVANT


def test_all_batches_announcement_is_relevant():
    decision = is_relevant("This applies to all batches", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.RELEVANT


def test_all_sections_overrides_a_conflicting_specific_mention():
    # Explicit "applies to everyone" is treated as the stronger signal.
    decision = is_relevant("For all sections", "Originally posted for Section D", "OOP Theory", _profile())
    assert decision == RelevanceDecision.RELEVANT


def test_bare_plural_sections_word_does_not_suppress():
    # Regression: "sections" alone must not be misread as "Section S" and
    # must not spuriously trigger OTHER_SECTION.
    decision = is_relevant("See the attached sections.", "", "OOP Theory", _profile())
    assert decision != RelevanceDecision.OTHER_SECTION


# --- The five decided ambiguous cases (docs/INTERFACES.md contract #3) ---


def test_chapter_reference_is_unknown():
    decision = is_relevant("Read Section 3 of the textbook", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.UNKNOWN


def test_course_code_is_unknown():
    decision = is_relevant("CS-101 Assignment", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.UNKNOWN


def test_range_is_unknown():
    decision = is_relevant("Sections A-D", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.UNKNOWN


def test_no_section_token_is_unknown():
    decision = is_relevant("Assignment 1 is due Monday", "", "OOP Theory", _profile())
    assert decision == RelevanceDecision.UNKNOWN


def test_title_description_disagreement_is_unknown():
    decision = is_relevant("Section C", "This is actually for Section D", "OOP Theory", _profile())
    assert decision == RelevanceDecision.UNKNOWN


# --- Property invariant: only OTHER_SECTION ever suppresses notification ---


@pytest.mark.parametrize(
    "title,description,course_name,expect_suppressed",
    [
        ("Lab 02 Section C", "", "OOP Theory", False),
        ("Lab 02 Section D", "", "OOP Theory", True),
        ("Read Section 3 of the textbook", "", "OOP Theory", False),
        ("CS-101 Assignment", "", "OOP Theory", False),
        ("Sections A-D", "", "OOP Theory", False),
        ("Assignment 1 is due Monday", "", "OOP Theory", False),
        ("Section C", "This is actually for Section D", "OOP Theory", False),
        ("Lab 02 Section D", "", "Some Other Course", False),
        ("", "", "OOP Theory", False),
        ("Section D & E", "", "OOP Theory", True),
    ],
)
def test_no_input_yields_notify_false_except_other_section(title, description, course_name, expect_suppressed):
    # The property invariant (docs/INTERFACES.md contract #3): OTHER_SECTION
    # is the only decision that suppresses notification. This asserts the
    # exact suppress/no-suppress boundary across the full decided corpus.
    decision = is_relevant(title, description, course_name, _profile())
    assert (decision == RelevanceDecision.OTHER_SECTION) == expect_suppressed


# --- expected_semester must never influence the decision (design decision:
# a frozen/repeated/delayed course must never be suppressed for being from
# an earlier catalog semester than expected_semester) ---


@pytest.mark.parametrize("expected_semester", [1, 2, 3, 4, 5, 8])
def test_expected_semester_never_changes_relevant_decision(expected_semester):
    decision = is_relevant("Lab 02 Section C", "", "OOP Theory", _profile(expected_semester=expected_semester))
    assert decision == RelevanceDecision.RELEVANT


@pytest.mark.parametrize("expected_semester", [1, 2, 3, 4, 5, 8])
def test_expected_semester_never_changes_other_section_decision(expected_semester):
    decision = is_relevant("Lab 02 Section D", "", "OOP Theory", _profile(expected_semester=expected_semester))
    assert decision == RelevanceDecision.OTHER_SECTION


def test_repeat_course_from_earlier_semester_is_not_suppressed():
    # Real data: OOP Theory is a REPEAT course (ragra/timetable/enrollment.py),
    # from an earlier catalog semester than a semester-3 student would
    # normally be taking it. expected_semester must not suppress it.
    profile = load_profile(today=date(2026, 9, 1))
    assert profile.expected_semester == 3
    assert profile.section_labels["OOP Theory"] == "CS-C"

    decision = is_relevant("Lab 02 Section C", "", "OOP Theory", profile)
    assert decision == RelevanceDecision.RELEVANT
