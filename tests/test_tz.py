"""Tests for ragra.tz - the single wall-clock <-> instant boundary.

The DST cases are deliberately exercised against America/New_York rather
than Asia/Karachi. Pakistan currently has no DST, so testing the transition
branches against the campus zone would leave them unexecuted and the tests
vacuously green - which is exactly how a timezone bug survives its own test
suite.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ragra.tz import (
    TimezoneDataUnavailable,
    campus_zone,
    combine_local,
    format_local,
    is_ambiguous,
    is_nonexistent,
    local_day_bounds,
    parse_instant,
    require_aware,
    to_local,
    to_utc,
    utc_iso,
)

KARACHI = ZoneInfo("Asia/Karachi")
NEW_YORK = ZoneInfo("America/New_York")

# US DST transitions in 2026: forward 2026-03-08 02:00, back 2026-11-01 02:00.
SPRING_FORWARD_GAP = (date(2026, 3, 8), time(2, 30))
FALL_BACK_OVERLAP = (date(2026, 11, 1), time(1, 30))


def test_campus_zone_resolves_by_name():
    assert campus_zone("Asia/Karachi").key == "Asia/Karachi"


def test_campus_zone_fails_loudly_for_missing_zone_data():
    # Never silently falls back to a fixed offset.
    with pytest.raises(TimezoneDataUnavailable):
        campus_zone("Not/ARealZone")


def test_naive_datetimes_are_rejected_at_the_boundary():
    with pytest.raises(ValueError):
        require_aware(datetime(2026, 9, 2, 8, 30))
    with pytest.raises(ValueError):
        to_utc(datetime(2026, 9, 2, 8, 30))


def test_combine_local_produces_campus_wall_clock():
    local = combine_local(date(2026, 9, 2), time(8, 30), zone=KARACHI)
    assert local.hour == 8 and local.minute == 30
    assert local.utcoffset() == timedelta(hours=5)
    assert to_utc(local) == datetime(2026, 9, 2, 3, 30, tzinfo=timezone.utc)


def test_utc_iso_is_always_the_plus_zero_form():
    # 'Z' and '+00:00' must never be mixed - lexicographic SQL ordering
    # depends on one canonical form ('+' sorts before 'Z').
    text = utc_iso(datetime(2026, 9, 2, 3, 30, tzinfo=timezone.utc))
    assert text.endswith("+00:00")
    assert "Z" not in text


def test_parse_instant_accepts_both_stored_forms_as_the_same_instant():
    ragra_form = parse_instant("2026-09-11T23:59:00+00:00")
    classroom_form = parse_instant("2026-09-11T23:59:00Z")
    assert ragra_form == classroom_form


def test_parse_instant_rejects_a_timestamp_without_an_offset():
    with pytest.raises(ValueError):
        parse_instant("2026-09-11T23:59:00")


# --- DST transitions, exercised against a zone that actually has them ---


def test_nonexistent_local_time_is_detected():
    day, slot = SPRING_FORWARD_GAP
    naive_in_gap = datetime.combine(day, slot).replace(tzinfo=NEW_YORK)
    assert is_nonexistent(naive_in_gap)


def test_nonexistent_local_time_shifts_forward_past_the_gap():
    day, slot = SPRING_FORWARD_GAP
    resolved = combine_local(day, slot, zone=NEW_YORK)

    assert not is_nonexistent(resolved)
    # 02:30 does not exist that day; it lands on 03:30, the first real
    # instant after the transition.
    assert (resolved.hour, resolved.minute) == (3, 30)


def test_ambiguous_local_time_is_detected():
    day, slot = FALL_BACK_OVERLAP
    overlapping = datetime.combine(day, slot).replace(tzinfo=NEW_YORK)
    assert is_ambiguous(overlapping)


def test_ambiguous_local_time_resolves_to_the_first_occurrence():
    day, slot = FALL_BACK_OVERLAP
    resolved = combine_local(day, slot, zone=NEW_YORK)

    assert (resolved.hour, resolved.minute) == (1, 30)
    assert resolved.fold == 0
    # The earlier of the two 01:30s - EDT (-04:00), not EST (-05:00).
    assert resolved.utcoffset() == timedelta(hours=-4)


def test_local_day_bounds_survive_a_dst_transition_day():
    # The spring-forward day is only 23 hours long; the bounds must reflect
    # that rather than assuming a fixed 24-hour day.
    day, _ = SPRING_FORWARD_GAP
    midday = combine_local(day, time(12, 0), zone=NEW_YORK)
    start, end = local_day_bounds(midday, zone=NEW_YORK)

    assert (end - start) < timedelta(hours=24)
    assert to_local(start, zone=NEW_YORK).date() == day
    assert to_local(end, zone=NEW_YORK).date() == day


# --- The local-day boundary that "today" depends on ---


def test_local_day_bounds_cover_the_campus_day_not_the_utc_day():
    # 20:00 UTC is already the next campus day (01:00 PKT), which is exactly
    # the boundary the brief used to get wrong.
    instant = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
    start, end = local_day_bounds(instant, zone=KARACHI)

    assert to_local(instant, zone=KARACHI).date() == date(2026, 9, 3)
    assert to_local(start, zone=KARACHI).date() == date(2026, 9, 3)
    assert to_local(end, zone=KARACHI).date() == date(2026, 9, 3)
    assert start <= instant <= end


def test_local_day_bounds_are_inclusive_of_the_whole_day():
    instant = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)
    start, end = local_day_bounds(instant, zone=KARACHI)

    assert to_local(start, zone=KARACHI).time() == time(0, 0)
    assert to_local(end, zone=KARACHI).hour == 23
    assert to_local(end, zone=KARACHI).minute == 59


# --- Display ---


def test_format_local_is_always_zone_labelled():
    # An unlabelled time in the wrong zone looks perfectly plausible; the
    # label is what makes a bad conversion visible to a human.
    text = format_local(datetime(2026, 9, 11, 23, 59, tzinfo=timezone.utc), zone=KARACHI)
    assert "PKT" in text


def test_format_local_converts_to_campus_time():
    # A deadline stored as 23:59 UTC is really 04:59 the next campus morning.
    text = format_local(datetime(2026, 9, 11, 23, 59, tzinfo=timezone.utc), zone=KARACHI)
    assert "12 Sep" in text
    assert "4:59 AM" in text
