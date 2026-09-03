"""UserAcademicProfile: the single source of truth for who a student is,
used for section-relevance matching and FAST timetable enrollment.

Storage is per user (migration 0024). Before P3 this was a hand-edited
module constant, which is correct for one user and wrong for several: a
constant has no owner, so a second account would inherit the first one's
enrollment and be told which of someone else's classes matter to them.

The fallback rule is deliberately narrow. A user with no stored profile
gets the module default *only* if they are the pre-identity owner - the
account whose configuration has always lived in
ragra/timetable/enrollment.py. Everyone else gets an empty profile, which
degrades safely in both places it is consumed: relevance falls open
(nothing is suppressed) and timetable matching finds no classes rather
than somebody else's. Silently handing a new user another person's
enrollment would be the far worse failure, and it is the one a permissive
default produces.

See docs/INTERFACES.md contract #4.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from ragra.timetable.enrollment import (
    ENROLLMENT_START_TERM,
    ENROLLMENT_START_YEAR,
    MY_ENROLLMENT,
    TARGET_BATCH_YEAR,
    TARGET_PROGRAM,
    EnrolledCourse,
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


def _build(
    *,
    program: str,
    start_year: int,
    start_term: str,
    enrollment: tuple[EnrolledCourse, ...],
    today: date,
) -> UserAcademicProfile:
    return UserAcademicProfile(
        program=program,
        expected_semester=_expected_semester(start_year, start_term, today=today),
        enrolled_courses=[course.course_name for course in enrollment],
        section_labels={course.course_name: course.section for course in enrollment},
        enrollment_config={"enrollment": enrollment},
    )


def _legacy_profile(today: date) -> UserAcademicProfile:
    """The configuration that has always lived in
    ragra/timetable/enrollment.py, belonging to the pre-identity owner."""
    return _build(
        program=TARGET_PROGRAM,
        start_year=ENROLLMENT_START_YEAR,
        start_term=ENROLLMENT_START_TERM,
        enrollment=MY_ENROLLMENT,
        today=today,
    )


def _empty_profile(today: date) -> UserAcademicProfile:
    """A user who has not configured anything yet.

    Empty rather than absent so every consumer keeps working: relevance
    finds no section labels and therefore suppresses nothing, and timetable
    matching finds no enrolled courses and therefore stores no classes.
    Both are the safe direction."""
    return _build(
        program="",
        start_year=today.year,
        start_term=_term_for_date(today)[1],
        enrollment=(),
        today=today,
    )


def _enrollment_from_json(raw: str) -> tuple[EnrolledCourse, ...]:
    """Rebuild the enrollment tuple from stored JSON.

    Unknown keys are ignored rather than raising: a profile written by a
    later version must not make an older one unable to read a user's
    account at all. EnrolledCourse's own validation still applies, so a
    malformed enrollment_type is still rejected.
    """
    entries = json.loads(raw)
    known = {"course_name", "section", "enrollment_type", "batch_year", "aliases"}
    courses = []
    for entry in entries:
        fields = {key: value for key, value in entry.items() if key in known}
        fields["aliases"] = tuple(fields.get("aliases") or ())
        courses.append(EnrolledCourse(**fields))
    return tuple(courses)


def _enrollment_to_json(enrollment: tuple[EnrolledCourse, ...]) -> str:
    return json.dumps(
        [
            {
                "course_name": course.course_name,
                "section": course.section,
                "enrollment_type": course.enrollment_type,
                "batch_year": course.batch_year,
                "aliases": list(course.aliases),
            }
            for course in enrollment
        ]
    )


def load_profile(
    conn: sqlite3.Connection | None = None,
    *,
    user_id: int | None = None,
    today: date | None = None,
) -> UserAcademicProfile:
    """The academic profile for one user.

    `conn` and `user_id` are optional so the pure, date-only behaviour
    stays available to callers that genuinely have no database - the
    relevance engine's own tests, for instance. Passing neither returns the
    legacy profile, which is what every pre-P3 caller got.

    `today` is injectable for deterministic testing; defaults to
    date.today().
    """
    resolved_today = today if today is not None else date.today()

    if conn is None or user_id is None:
        return _legacy_profile(resolved_today)

    row = conn.execute(
        "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None:
        return _build(
            program=row["program"],
            start_year=row["enrollment_start_year"],
            start_term=row["enrollment_start_term"],
            enrollment=_enrollment_from_json(row["enrollment"]),
            today=resolved_today,
        )

    from ragra.db import repo

    if repo.unlinked_user_id(conn) == user_id:
        return _legacy_profile(resolved_today)
    return _empty_profile(resolved_today)


@dataclass(frozen=True)
class RawProfile:
    """The stored fields of a profile, exactly as saved - as opposed to
    `UserAcademicProfile`, which is the *derived* shape relevance matching
    and timetable sync consume (it computes `expected_semester` and does
    not carry `enrollment_start_year`/`enrollment_start_term` at all, since
    nothing downstream of `load_profile` needs the raw start term once
    `expected_semester` has been computed from it).

    This exists for exactly one purpose: an editor needs to redisplay what
    a user actually saved, including the start year/term `load_profile`
    deliberately discards after using them. Keeping that need out of
    `UserAcademicProfile` is what lets that dataclass's signature keep the
    stability docs/INTERFACES.md contract #4 commits to.
    """

    program: str
    batch_year: str | None
    enrollment_start_year: int
    enrollment_start_term: str
    enrollment: tuple[EnrolledCourse, ...]


def load_raw_profile(conn: sqlite3.Connection, *, user_id: int) -> RawProfile:
    """The stored fields for one user, for an editor to redisplay.

    Follows the same fallback rule as `load_profile` (see its docstring):
    the module default only for the pre-identity owner, empty otherwise -
    so a new user's editor opens on a blank profile, never somebody else's.
    """
    row = conn.execute(
        "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None:
        return RawProfile(
            program=row["program"],
            batch_year=row["batch_year"],
            enrollment_start_year=row["enrollment_start_year"],
            enrollment_start_term=row["enrollment_start_term"],
            enrollment=_enrollment_from_json(row["enrollment"]),
        )

    from ragra.db import repo

    if repo.unlinked_user_id(conn) == user_id:
        return RawProfile(
            program=TARGET_PROGRAM,
            batch_year=TARGET_BATCH_YEAR,
            enrollment_start_year=ENROLLMENT_START_YEAR,
            enrollment_start_term=ENROLLMENT_START_TERM,
            enrollment=MY_ENROLLMENT,
        )

    today = date.today()
    return RawProfile(
        program="",
        batch_year=None,
        enrollment_start_year=today.year,
        enrollment_start_term=_term_for_date(today)[1],
        enrollment=(),
    )


def save_profile(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    program: str,
    batch_year: str | None,
    enrollment_start_year: int,
    enrollment_start_term: str,
    enrollment: tuple[EnrolledCourse, ...],
) -> None:
    """Write one user's profile. Replaces rather than merges: a profile is
    edited as a whole, and a partial write would leave an enrollment half
    from one semester and half from the next."""
    from ragra.db.repo import now_iso

    if enrollment_start_term not in (_FALL, _SPRING):
        raise ValueError(f"enrollment_start_term must be {_FALL!r} or {_SPRING!r}")

    now = now_iso()
    conn.execute(
        """INSERT INTO user_profiles
             (user_id, program, batch_year, enrollment_start_year, enrollment_start_term,
              enrollment, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             program = excluded.program,
             batch_year = excluded.batch_year,
             enrollment_start_year = excluded.enrollment_start_year,
             enrollment_start_term = excluded.enrollment_start_term,
             enrollment = excluded.enrollment,
             updated_at = excluded.updated_at""",
        (
            user_id, program, batch_year, enrollment_start_year, enrollment_start_term,
            _enrollment_to_json(enrollment), now, now,
        ),
    )
    conn.commit()


def adopt_legacy_profile(conn: sqlite3.Connection, *, user_id: int) -> None:
    """Persist the module-default profile as a real row for one user.

    Used when the pre-identity owner signs in and becomes a normal account:
    once they do, the "are you the unlinked user?" fallback above no longer
    matches them, so their configuration has to exist as data before that
    happens rather than after.
    """
    save_profile(
        conn,
        user_id=user_id,
        program=TARGET_PROGRAM,
        batch_year=TARGET_BATCH_YEAR,
        enrollment_start_year=ENROLLMENT_START_YEAR,
        enrollment_start_term=ENROLLMENT_START_TERM,
        enrollment=MY_ENROLLMENT,
    )
