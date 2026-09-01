"""is_relevant(): decide whether a piece of Classroom content belongs to
this student's section. Deterministic pattern matching only - no
inference, no expansion, no AI judgment. See docs/INTERFACES.md contract
#3 for the decided semantics and the property-test invariant this module
must uphold: no input ever yields notify=False except OTHER_SECTION.
"""

from __future__ import annotations

from enum import Enum

from ragra.relevance.profile import UserAcademicProfile
from ragra.relevance.sections import extract_sections
from ragra.timetable.normalize import normalize_course_text, normalize_section


class RelevanceDecision(Enum):
    RELEVANT = "relevant"
    OTHER_SECTION = "other_section"
    UNKNOWN = "unknown"


def _find_course_key(course_name: str, profile: UserAcademicProfile) -> str | None:
    """Match a Classroom course name against the profile's own FAST course
    names. No crosswalk exists between the two systems (see
    docs/INTERFACES.md contract #4), so this is a tolerant normalized
    substring match rather than an exact key lookup."""
    target = normalize_course_text(course_name)
    for known in profile.section_labels:
        known_norm = normalize_course_text(known)
        if known_norm == target or known_norm in target or target in known_norm:
            return known
    return None


def _section_letter(label: str) -> str:
    """The trailing section letter of a stored "CS-C"-style label."""
    return normalize_section(label).rsplit("-", 1)[-1]


def is_relevant(
    content_title: str,
    content_description: str,
    course_name: str,
    profile: UserAcademicProfile,
) -> RelevanceDecision:
    """Determine whether this Classroom content belongs to this student.
    profile.expected_semester is never read here - see docs/INTERFACES.md
    contract #4: it is descriptive metadata only, never an eligibility
    signal."""
    title_extraction = extract_sections(content_title)
    description_extraction = extract_sections(content_description or "")

    if title_extraction.applies_to_all or description_extraction.applies_to_all:
        return RelevanceDecision.RELEVANT

    if title_extraction.is_range or description_extraction.is_range:
        return RelevanceDecision.UNKNOWN

    title_sections = set(title_extraction.sections)
    description_sections = set(description_extraction.sections)

    if title_sections and description_sections and title_sections != description_sections:
        return RelevanceDecision.UNKNOWN

    mentioned = title_sections or description_sections
    if not mentioned:
        return RelevanceDecision.UNKNOWN

    course_key = _find_course_key(course_name, profile)
    if course_key is None:
        return RelevanceDecision.RELEVANT

    expected_label = profile.section_labels.get(course_key)
    if not expected_label:
        return RelevanceDecision.RELEVANT

    if _section_letter(expected_label) in mentioned:
        return RelevanceDecision.RELEVANT

    return RelevanceDecision.OTHER_SECTION
