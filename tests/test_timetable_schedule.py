"""Tests for ragra.timetable.schedule - weekly pattern -> concrete
occurrences, computed on demand.

DST behaviour is exercised against America/New_York, not Asia/Karachi:
Pakistan has no DST today, so testing transitions against the campus zone
would leave those branches unexecuted while still passing.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ragra.timetable.schedule import (
    CANCELLED,
    MalformedTimetableTime,
    WeeklyClass,
    expand_occurrences,
    weekly_class_from_row,
)
from ragra.tz import to_utc

KARACHI = ZoneInfo("Asia/Karachi")
NEW_YORK = ZoneInfo("America/New_York")


def _monday_class(**overrides) -> WeeklyClass:
    defaults = dict(
        timetable_event_id=1,
        course_name="DLD",
        day_of_week=0,  # Monday
        start_time="08:30",
        end_time="09:50",
        section="CS-G",
        room="C-311",
    )
    defaults.update(overrides)
    return WeeklyClass(**defaults)


def _window(start_utc: datetime, days: int) -> tuple[datetime, datetime]:
    return start_utc, start_utc + timedelta(days=days)


def test_weekly_pattern_expands_to_concrete_occurrences():
    # 2026-09-07 is a Monday.
    start, end = _window(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc), days=14)
    occurrences = expand_occurrences([_monday_class()], window_start=start, window_end=end, zone=KARACHI)

    # The 21st's class starts 03:30 UTC, just past the end of a 14-day
    # window that closes at 00:00 UTC - correctly excluded.
    assert [o.occurrence_date for o in occurrences] == [date(2026, 9, 7), date(2026, 9, 14)]


def test_occurrence_carries_both_local_and_utc_and_they_agree():
    start, end = _window(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc), days=1)
    occurrence = expand_occurrences([_monday_class()], window_start=start, window_end=end, zone=KARACHI)[0]

    assert occurrence.starts_at_local.hour == 8
    assert occurrence.starts_at_local.minute == 30
    assert occurrence.starts_at_utc == to_utc(occurrence.starts_at_local)
    assert occurrence.starts_at_utc == datetime(2026, 9, 7, 3, 30, tzinfo=timezone.utc)


def test_every_returned_datetime_is_timezone_aware():
    start, end = _window(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc), days=7)
    for occurrence in expand_occurrences([_monday_class()], window_start=start, window_end=end, zone=KARACHI):
        for value in (
            occurrence.starts_at_local, occurrence.starts_at_utc,
            occurrence.ends_at_local, occurrence.ends_at_utc,
        ):
            assert value.tzinfo is not None


def test_window_bounds_are_respected():
    # A window starting Tuesday must skip that week's Monday and pick up
    # only the following one.
    start, end = _window(datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc), days=7)
    occurrences = expand_occurrences([_monday_class()], window_start=start, window_end=end, zone=KARACHI)

    assert [o.occurrence_date for o in occurrences] == [date(2026, 9, 14)]


def test_narrow_window_yields_nothing_rather_than_guessing():
    start = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    occurrences = expand_occurrences(
        [_monday_class()], window_start=start, window_end=start + timedelta(hours=1), zone=KARACHI
    )
    assert occurrences == []


def test_cancelled_class_is_returned_but_flagged():
    # Returned so the dashboard can show it; callers that schedule anything
    # must filter on is_cancelled.
    start, end = _window(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc), days=1)
    occurrence = expand_occurrences(
        [_monday_class(status=CANCELLED)], window_start=start, window_end=end, zone=KARACHI
    )[0]

    assert occurrence.is_cancelled


def test_occurrences_are_sorted_by_instant():
    early = _monday_class(timetable_event_id=1, course_name="Early", start_time="08:30", end_time="09:50")
    late = _monday_class(timetable_event_id=2, course_name="Late", start_time="14:00", end_time="15:20")
    start, end = _window(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc), days=1)

    occurrences = expand_occurrences([late, early], window_start=start, window_end=end, zone=KARACHI)

    assert [o.course_name for o in occurrences] == ["Early", "Late"]


def test_block_running_past_midnight_ends_on_the_next_day():
    overnight = _monday_class(start_time="23:00", end_time="00:30")
    start, end = _window(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc), days=1)

    occurrence = expand_occurrences([overnight], window_start=start, window_end=end, zone=KARACHI)[0]

    assert occurrence.ends_at_utc > occurrence.starts_at_utc
    assert occurrence.ends_at_local.date() == date(2026, 9, 8)


def test_malformed_time_raises_rather_than_being_coerced():
    start, end = _window(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc), days=1)
    with pytest.raises(MalformedTimetableTime):
        expand_occurrences(
            [_monday_class(start_time="not-a-time")], window_start=start, window_end=end, zone=KARACHI
        )


def test_naive_window_bounds_are_rejected():
    with pytest.raises(ValueError):
        expand_occurrences(
            [_monday_class()],
            window_start=datetime(2026, 9, 7, 0, 0),
            window_end=datetime(2026, 9, 8, 0, 0),
            zone=KARACHI,
        )


def test_reversed_window_is_rejected():
    start = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        expand_occurrences(
            [_monday_class()], window_start=start, window_end=start - timedelta(days=1), zone=KARACHI
        )


def test_weekly_class_from_row_adapts_a_row_mapping():
    row = {
        "id": 7, "course_name": "OOP Lab", "day_of_week": 2, "start_time": "11:30",
        "end_time": "14:15", "section": "CS-A", "room": "Lab-1", "status": "SCHEDULED",
    }
    weekly = weekly_class_from_row(row)

    assert weekly.timetable_event_id == 7
    assert weekly.course_name == "OOP Lab"
    assert not weekly.is_cancelled


# --- DST, against a zone that actually observes it ---


def test_class_keeps_its_wall_clock_time_across_a_dst_transition():
    # US DST starts 2026-03-08. A 09:00 Monday class must still be 09:00
    # local on both sides of the transition - its UTC instant shifts, not
    # its wall clock. Getting this backwards is the silent-wrong-time bug.
    monday_nine = _monday_class(start_time="09:00", end_time="10:20")
    start, end = _window(datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc), days=21)

    occurrences = expand_occurrences([monday_nine], window_start=start, window_end=end, zone=NEW_YORK)

    assert len(occurrences) >= 2
    assert {o.starts_at_local.hour for o in occurrences} == {9}
    utc_hours = {o.starts_at_utc.hour for o in occurrences}
    assert len(utc_hours) == 2  # the instant moves even though the wall clock does not


def test_class_scheduled_inside_the_spring_forward_gap_is_shifted_forward():
    # 2026-03-08 02:30 America/New_York does not exist.
    sunday_gap = _monday_class(day_of_week=6, start_time="02:30", end_time="03:50")
    start, end = _window(datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc), days=1)

    occurrences = expand_occurrences([sunday_gap], window_start=start, window_end=end, zone=NEW_YORK)

    assert len(occurrences) == 1
    assert occurrences[0].starts_at_local.hour == 3  # moved past the gap, not dropped
