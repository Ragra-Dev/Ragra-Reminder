"""Class-occurrence expansion: weekly recurrence pattern -> concrete
instants, computed on demand for a bounded window.

Nothing here is persisted. `timetable_events` rows are recurrence *rules*
(a weekday plus a campus wall-clock time), not instants, and materialising a
semester of occurrence rows would require semester boundary dates Ragra does
not have and must not invent - as well as creating a staleness problem the
moment a timetable cell changes or a timezone rule is updated. Expanding on
demand makes both problems disappear: the answer is always derived from the
current pattern and the current timezone database.

Pure by construction: no sqlite3, no network, no AI. Every returned datetime
is timezone-aware, and each occurrence carries both its campus-local form
(for display) and its UTC instant (for comparison and scheduling) so a
caller can never accidentally use one where the other is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from ragra.tz import campus_zone, combine_local, require_aware, to_local, to_utc

SCHEDULED = "SCHEDULED"
CANCELLED = "CANCELLED"


class MalformedTimetableTime(ValueError):
    """A stored timetable time is not 'HH:MM'. Raised rather than guessed
    at - a silently coerced time produces a class reminder at the wrong
    hour, which is worse than a loud failure."""


@dataclass(frozen=True)
class WeeklyClass:
    """One weekly meeting pattern, straight from timetable_events."""

    timetable_event_id: int
    course_name: str
    day_of_week: int  # 0=Monday .. 6=Sunday
    start_time: str  # HH:MM, campus wall-clock
    end_time: str  # HH:MM, campus wall-clock
    section: str | None = None
    room: str | None = None
    status: str = SCHEDULED

    @property
    def is_cancelled(self) -> bool:
        return self.status == CANCELLED


@dataclass(frozen=True)
class ClassOccurrence:
    """One concrete meeting of a class. Carries both representations
    deliberately: `starts_at_utc` for every comparison and scheduling
    decision, `starts_at_local` for anything a human reads."""

    timetable_event_id: int
    course_name: str
    occurrence_date: date  # campus-local date; part of the reminder identity
    starts_at_local: datetime
    starts_at_utc: datetime
    ends_at_local: datetime
    ends_at_utc: datetime
    section: str | None
    room: str | None
    status: str

    @property
    def is_cancelled(self) -> bool:
        return self.status == CANCELLED


def weekly_class_from_row(row: Any) -> WeeklyClass:
    """Adapt a timetable_events row (sqlite3.Row or any mapping) without
    importing sqlite3 into this module."""
    return WeeklyClass(
        timetable_event_id=row["id"],
        course_name=row["course_name"],
        day_of_week=row["day_of_week"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        section=row["section"],
        room=row["room"],
        status=row["status"],
    )


def _parse_hhmm(value: str, *, field: str) -> time:
    try:
        hour_text, minute_text = value.strip().split(":")
        return time(int(hour_text), int(minute_text))
    except (AttributeError, ValueError) as exc:
        raise MalformedTimetableTime(f"{field}={value!r} is not a valid HH:MM time") from exc


def expand_occurrences(
    classes: Sequence[WeeklyClass],
    *,
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo | None = None,
) -> list[ClassOccurrence]:
    """Every occurrence whose *start* falls within [window_start, window_end],
    ordered by instant. Both bounds must be timezone-aware; the window is
    interpreted as instants, while the recurrence is interpreted in campus
    local time - which is precisely the conversion this function exists to
    perform.

    Cancelled classes are still returned, carrying their status, so the
    dashboard can show that a class was cancelled. Callers that schedule
    anything must filter on `is_cancelled` - a cancelled class must never
    produce a reminder.
    """
    require_aware(window_start, what="window_start")
    require_aware(window_end, what="window_end")
    if window_end < window_start:
        raise ValueError("window_end must not be before window_start")

    resolved = zone or campus_zone()
    first_local_day = to_local(window_start, zone=resolved).date()
    last_local_day = to_local(window_end, zone=resolved).date()

    occurrences: list[ClassOccurrence] = []
    for weekly in classes:
        start_time = _parse_hhmm(weekly.start_time, field="start_time")
        end_time = _parse_hhmm(weekly.end_time, field="end_time")

        day = first_local_day
        while day <= last_local_day:
            if day.weekday() != weekly.day_of_week:
                day += timedelta(days=1)
                continue

            starts_local = combine_local(day, start_time, zone=resolved)
            starts_utc = to_utc(starts_local)
            if not (window_start <= starts_utc <= window_end):
                day += timedelta(days=1)
                continue

            # A block whose end is not after its start has run past midnight
            # (FAST's own 12-hour source occasionally produces this before
            # resolve_24h_time_sequence normalises it).
            end_day = day if end_time > start_time else day + timedelta(days=1)
            ends_local = combine_local(end_day, end_time, zone=resolved)

            occurrences.append(
                ClassOccurrence(
                    timetable_event_id=weekly.timetable_event_id,
                    course_name=weekly.course_name,
                    occurrence_date=day,
                    starts_at_local=starts_local,
                    starts_at_utc=starts_utc,
                    ends_at_local=ends_local,
                    ends_at_utc=to_utc(ends_local),
                    section=weekly.section,
                    room=weekly.room,
                    status=weekly.status,
                )
            )
            day += timedelta(days=1)

    occurrences.sort(key=lambda item: (item.starts_at_utc, item.course_name))
    return occurrences


def occurrences_for_local_day(
    classes: Sequence[WeeklyClass],
    *,
    instant: datetime,
    zone: ZoneInfo | None = None,
) -> list[ClassOccurrence]:
    """Every class on the campus calendar day containing `instant`.

    "Today" here means the local day, not the UTC one - for five hours of
    every day those disagree, and a schedule that shows yesterday's classes
    after 7pm would be worse than showing none.
    """
    from ragra.tz import local_day_bounds

    resolved = zone or campus_zone()
    day_start, day_end = local_day_bounds(instant, zone=resolved)
    return expand_occurrences(
        classes, window_start=day_start, window_end=day_end, zone=resolved
    )
