"""IDENTIFY stage: match a parsed timetable cell against the user's own
enrollment config (ragra/timetable/enrollment.py). Deliberately separate
from both scraping (ragra/adapters/fast_timetable.py) and persistence
(ragra/sync/timetable_sync.py) - this module only ever takes plain data in
and returns plain data out, so it can be unit-tested without a network
connection or a database.

Matching is by course-name/alias + section only, both normalized. It is
never influenced by which column-group a cell sits under, and never by
color - the enrollment table itself is the single source of truth for
regular vs. repeat.
"""

from __future__ import annotations

from dataclasses import dataclass

from ragra.timetable.enrollment import EnrolledCourse
from ragra.timetable.normalize import ParsedCell, normalize_course_text, normalize_section


@dataclass(frozen=True)
class MatchedClass:
    enrolled: EnrolledCourse
    day_of_week: int
    start_time: str
    end_time: str
    room: str | None
    cancelled: bool


def _course_candidates(enrolled: EnrolledCourse) -> set[str]:
    return {normalize_course_text(enrolled.course_name)} | {
        normalize_course_text(alias) for alias in enrolled.aliases
    }


def match_cell(
    parsed: ParsedCell,
    enrollment: tuple[EnrolledCourse, ...],
    *,
    day_of_week: int,
    start_time: str,
    end_time: str,
    room: str | None,
) -> MatchedClass | None:
    """Return the single EnrolledCourse this cell corresponds to, or None if
    it matches none of the user's enrolled courses (e.g. it's someone
    else's section). Raises AmbiguousMatchError if more than one enrolled
    row matches equally - that must be reported, never silently resolved by
    picking one."""
    cell_course = normalize_course_text(parsed.course_text)
    cell_section = normalize_section(parsed.section)

    matches = [
        enrolled
        for enrolled in enrollment
        if cell_course in _course_candidates(enrolled) and normalize_section(enrolled.section) == cell_section
    ]

    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousMatchError(
            f"cell (course={parsed.course_text!r}, section={parsed.section!r}) matches "
            f"{len(matches)} enrollment rows: {[m.course_name for m in matches]}"
        )

    return MatchedClass(
        enrolled=matches[0],
        day_of_week=day_of_week,
        start_time=start_time,
        end_time=end_time,
        room=room,
        cancelled=parsed.cancelled,
    )


class AmbiguousMatchError(RuntimeError):
    """Raised when a single timetable cell matches more than one enrolled
    course+section - a real configuration problem to surface, not to guess
    past."""
