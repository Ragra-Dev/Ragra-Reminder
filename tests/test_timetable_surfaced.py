"""Tests for the timetable being surfaced in the dashboard and the brief.

Closes ROADMAP finding #4: the timetable was synced and persisted but never
read by anything the user sees.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ragra.brief import build_deterministic_brief
from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.timetable.schedule import occurrences_for_local_day, weekly_class_from_row
from ragra.web.app import create_app

# 2026-09-07 is a Monday; 08:30 PKT == 03:30 UTC.
MONDAY_MIDDAY_UTC = datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)


def _add_class(conn, **overrides):
    defaults = dict(
        external_id="tt-1", course_name="DLD", program="CS", batch_year="2025",
        enrollment_type="REGULAR", day_of_week=0, occurrence_index=0,
        start_time="08:30", end_time="09:50", room="C-311", instructor=None,
        section="CS-G", status="SCHEDULED", source_spreadsheet_id="sheet-1",
        source_sheet_gid="1", source_sheet_title="Monday",
    )
    defaults.update(overrides)
    repo.upsert_timetable_event(conn, **defaults)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "web.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))


def test_occurrences_for_local_day_returns_that_days_classes(conn):
    _add_class(conn)
    _add_class(conn, external_id="tt-2", course_name="OOP", day_of_week=1)

    classes = occurrences_for_local_day(
        [weekly_class_from_row(r) for r in repo.list_timetable_events(conn)],
        instant=MONDAY_MIDDAY_UTC,
    )

    assert [c.course_name for c in classes] == ["DLD"]  # Tuesday's class excluded


def test_brief_lists_todays_classes_in_campus_time(conn):
    _add_class(conn)

    text = build_deterministic_brief(conn, now=MONDAY_MIDDAY_UTC)

    assert "CLASSES TODAY (1):" in text
    assert "08:30-09:50" in text  # campus wall clock, not the 03:30 UTC instant
    assert "DLD" in text
    assert "C-311" in text


def test_brief_reports_no_classes_rather_than_omitting_the_section(conn):
    text = build_deterministic_brief(conn, now=MONDAY_MIDDAY_UTC)

    assert "CLASSES TODAY (0):" in text
    assert "(none)" in text


def test_brief_marks_a_cancelled_class(conn):
    _add_class(conn, status="CANCELLED")

    text = build_deterministic_brief(conn, now=MONDAY_MIDDAY_UTC)

    assert "[CANCELLED]" in text


def test_brief_survives_a_malformed_timetable_time(conn):
    # A bad stored time must degrade the schedule section, never take down
    # the deadline facts that make up the rest of the brief.
    _add_class(conn, start_time="not-a-time")

    text = build_deterministic_brief(conn, now=MONDAY_MIDDAY_UTC)

    assert "CLASSES TODAY (0):" in text
    assert "OVERDUE (0):" in text  # the rest of the brief is intact


def test_dashboard_shows_a_schedule_section(client, db_path):
    with connect_closing(db_path) as conn:
        _add_class(conn)

    body = client.get("/").text

    assert 'id="schedule"' in body


def test_dashboard_has_all_five_restructured_sections(client):
    body = client.get("/").text

    for anchor in ('id="schedule"', 'id="announcements"', 'id="tasks"', 'id="deadlines"'):
        assert anchor in body
    assert "<h2>Missed" in body


def test_dashboard_navigation_links_every_section(client):
    body = client.get("/").text

    for href in ('href="/tasks"', 'href="/announcements"', 'href="/missed"', 'href="/deliveries"'):
        assert href in body
