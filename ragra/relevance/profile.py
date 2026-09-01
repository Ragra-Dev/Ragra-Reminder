"""UserAcademicProfile: the single source of truth for who this student is,
for section-relevance matching and (eventually) multi-user config. Phase
0-1 is single-user, backed entirely by the hand-edited MY_ENROLLMENT table
in ragra/timetable/enrollment.py. See docs/INTERFACES.md contract #4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ragra.timetable.enrollment import (
    ENROLLMENT_START_TERM,
    ENROLLMENT_START_YEAR,
    MY_ENROLLMENT,
    TARGET_PROGRAM,
)

_FALL = "FALL"
_SPRING = "SPRING"


@dataclass
class UserAcademicProfile:
    program: str
    expected_semester: int
    enrolled_courses: list[str]
    section_labels: dict[str, str]
    enrollment_config: dict


def _term_for_date(today: date) -> tuple[int, str]:
    """Map a calendar date to (year, term) on FAST's two-term academic
    calendar. Ragra has no authoritative source for exact term boundaries,
    so this is a simple, documented approximation - acceptable only because
    expected_semester is purely descriptive metadata and never gates any
    decision (see docs/INTERFACES.md contract #4)."""
    return (today.year, _FALL) if today.month >= 7 else (today.year, _SPRING)


def _term_ordinal(year: int, term: str) -> int:
    # Spring of a calendar year runs before that year's Fall term.
    return year * 2 + (0 if term == _SPRING else 1)


def _expected_semester(start_year: int, start_term: str, *, today: date) -> int:
    """1-indexed term count from `start_term`/`start_year` through `today`'s
    term, inclusive of the start term."""
    current_year, current_term = _term_for_date(today)
    elapsed = _term_ordinal(current_year, current_term) - _term_ordinal(start_year, start_term)
    return elapsed + 1


def load_profile(user_id: str | None = None, *, today: date | None = None) -> UserAcademicProfile:
    """Load the academic profile for this user (or the default user if
    None). Phase 0-1: user_id is accepted but ignored; returns the
    hardcoded profile from ragra/timetable/enrollment.py. Phase 3 will
    fetch a row from a user_profiles table with this same signature.
    `today` is injectable for deterministic testing; defaults to
    date.today()."""
    resolved_today = today if today is not None else date.today()
    return UserAcademicProfile(
        program=TARGET_PROGRAM,
        expected_semester=_expected_semester(ENROLLMENT_START_YEAR, ENROLLMENT_START_TERM, today=resolved_today),
        enrolled_courses=[course.course_name for course in MY_ENROLLMENT],
        section_labels={course.course_name: course.section for course in MY_ENROLLMENT},
        enrollment_config={"enrollment": MY_ENROLLMENT},
    )
