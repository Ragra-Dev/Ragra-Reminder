"""Ragra's own enrollment model for matching FAST timetable entries.

Deliberately NOT hermes_cli.classroom.registration.py: that module lives
outside the narrow, already-decoupled Hermes surface Ragra imports (see
docs/ARCHITECTURE.md), and it solves a different problem - fuzzy-matching
noisy Classroom API course names against a known list. FAST timetable
matching is simpler: matching a precisely scraped (course, section) pair
from a spreadsheet cell against a small list the user maintains themselves.
This module follows registration.py's *pattern* (a small, hand-edited,
per-semester table) without importing it or its fuzzy-matching machinery.

Regular and repeat enrollment are both plain EnrolledCourse rows. There is
no shared "batch" object forcing a course's theory and lab sections to
match - each is independently configurable, and repeat theory/repeat lab
commonly use different sections. enrollment_type is explicit data on every
row; it is never inferred from a section letter, because FAST section
letters (A, B, C, G, ...) are arbitrary identifiers with no fixed
regular/repeat meaning - that meaning exists only in the user's own
enrollment, which is exactly what this table records.
"""

from __future__ import annotations

from dataclasses import dataclass

REGULAR = "REGULAR"
REPEAT = "REPEAT"


@dataclass(frozen=True)
class EnrolledCourse:
    course_name: str
    section: str
    enrollment_type: str  # REGULAR or REPEAT
    batch_year: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.enrollment_type not in (REGULAR, REPEAT):
            raise ValueError(
                f"enrollment_type must be {REGULAR!r} or {REPEAT!r}, got {self.enrollment_type!r}"
            )


# --- Current configuration: edit this each semester. Not parser logic. ---
# The parser (ragra/timetable/match.py) is entirely unaware of these values;
# it only ever consumes whatever EnrolledCourse tuple it's given.

TARGET_PROGRAM = "CS"
TARGET_BATCH_YEAR = "2025"

# Enrollment start term, used only to derive UserAcademicProfile.expected_semester
# (ragra/relevance/profile.py) - purely descriptive metadata, never an
# eligibility filter. See docs/INTERFACES.md contract #4 for why.
ENROLLMENT_START_YEAR = int(TARGET_BATCH_YEAR)
ENROLLMENT_START_TERM = "FALL"  # "FALL" or "SPRING"

MY_ENROLLMENT: tuple[EnrolledCourse, ...] = (
    EnrolledCourse("Linear Algebra", "CS-G", REGULAR, batch_year=TARGET_BATCH_YEAR, aliases=("LA",)),
    EnrolledCourse("DLD", "CS-G", REGULAR, batch_year=TARGET_BATCH_YEAR, aliases=("Digital Logic",)),
    EnrolledCourse(
        "DLD Lab", "CS-G", REGULAR, batch_year=TARGET_BATCH_YEAR, aliases=("Digital Logic Lab",)
    ),
    EnrolledCourse(
        "UHQ-I&II", "CS-G", REGULAR, batch_year=TARGET_BATCH_YEAR, aliases=("UHQ I&II", "UHQ-II", "UHQ")
    ),
    EnrolledCourse("Seerah", "CS-G", REGULAR, batch_year=TARGET_BATCH_YEAR),
    EnrolledCourse("Discrete Structures", "CS-B", REPEAT, aliases=("Discrete",)),
    EnrolledCourse("OOP Theory", "CS-C", REPEAT, aliases=("OOP",)),
    EnrolledCourse("OOP Lab", "CS-A", REPEAT),
)
