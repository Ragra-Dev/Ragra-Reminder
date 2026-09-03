from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect
from ragra.web.app import create_app

from tests.support import owner_id


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "web-test.db"
    conn = connect(db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment 2", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2020-01-01T00:00:00+00:00",
        source_published_at="2019-12-01T00:00:00+00:00",
        source_updated_at="2019-12-01T00:00:00+00:00", user_id=owner_id(conn),
    )
    conn.close()

    app = create_app(db_path)
    with TestClient(app) as c:
        c.task_id = result.task_id  # type: ignore[attr-defined]
        c.db_path = db_path  # type: ignore[attr-defined]
        yield c


def test_dashboard_loads_and_shows_overdue_task(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Assignment 2" in resp.text
    assert "overdue" in resp.text


def test_complete_task_removes_it_from_overdue(client):
    resp = client.post(f"/tasks/{client.task_id}/complete", follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get("/")
    # No longer flagged as overdue work needing action...
    overdue_section = resp.text.split("Due today</h2>")[0]
    assert "Assignment 2" not in overdue_section
    # ...but it should now surface in "Recently completed" (Phase 4:
    # the dashboard must be able to answer "what have I completed?").
    completed_section = resp.text.split("<h2>Recently completed</h2>")[1]
    assert "Assignment 2" in completed_section

    conn = connect(client.db_path)
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (client.task_id,)).fetchone()
    conn.close()
    assert row["status"] == "COMPLETED"


def test_dashboard_flags_deadlined_task_missing_personal_target(client):
    # The fixture's "Assignment 2" has an actual_deadline and no
    # personal_deadline - it should be flagged as needing a personal target
    # even though it already has an official Classroom due date.
    resp = client.get("/")
    assert "Has an academic deadline - when do you plan to actually do it?" in resp.text
    assert "no personal target set yet" in resp.text


def test_setting_personal_target_removes_deadlined_task_from_that_section(client):
    resp = client.post(
        f"/tasks/{client.task_id}/personal-deadline",
        data={"personal_deadline": "2019-12-28"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/")
    assert "no personal target set yet" not in resp.text
    assert "Every deadlined task already has a personal target." in resp.text


def test_due_today_and_due_soon_are_separate_sections(client):
    now = datetime.now(timezone.utc)
    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-today",
        title="Due Today Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(hours=2)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-soon",
        title="Due In Three Days Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(days=3)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get("/")
    today_section = resp.text.split("<h2>Due today</h2>")[1].split("<h2>Due soon")[0]
    soon_section = resp.text.split("<h2>Due soon")[1].split("<h2>Needs a personal")[0]

    assert "Due Today Task" in today_section
    assert "Due In Three Days Task" not in today_section
    assert "Due In Three Days Task" in soon_section
    assert "Due Today Task" not in soon_section


def test_scheduled_reminders_section_shows_upcoming_reminders(client):
    now = datetime.now(timezone.utc)
    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-r",
        title="Reminder Bearing Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(days=5)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    repo.insert_reminder_if_absent(
        conn, task_id=result.task_id, reminder_type="T_MINUS_1D",
        scheduled_for=(now + timedelta(days=4)).isoformat(), idempotency_key="k-upcoming", user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get("/")
    assert "Reminder Bearing Task" in resp.text
    assert "T_MINUS_1D" in resp.text
    assert "No reminders currently scheduled." not in resp.text


def _make_task(conn, *, external_id, title, status=None):
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id=external_id,
        title=title, description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2020-01-01T00:00:00+00:00",
        source_published_at="2019-12-01T00:00:00+00:00",
        source_updated_at="2019-12-01T00:00:00+00:00", user_id=owner_id(conn),
    )
    if status:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, result.task_id))
        conn.commit()
    return result.task_id


def test_missed_task_appears_in_missed_section(client):
    conn = connect(client.db_path)
    repo.mark_missed(conn, task_id=client.task_id, user_id=owner_id(conn))
    conn.close()

    resp = client.get("/")
    missed_section = resp.text.split("<h2>Missed")[1].split("Due today</h2>")[0]
    assert "Assignment 2" in missed_section
    assert "missed" in missed_section.lower()


def test_overdue_task_stays_in_overdue_section_and_not_in_missed(client):
    resp = client.get("/")
    overdue_section = resp.text.split("Overdue</h2>")[1].split("<h2>Missed")[0]
    missed_section = resp.text.split("<h2>Missed")[1].split("Due today</h2>")[0]
    assert "Assignment 2" in overdue_section
    assert "Assignment 2" not in missed_section


def test_completed_task_does_not_appear_in_overdue_or_missed(client):
    conn = connect(client.db_path)
    completed_id = _make_task(conn, external_id="cw-completed", title="Completed Long Ago")
    conn.close()
    conn = connect(client.db_path)
    repo.mark_completed(conn, task_id=completed_id, user_id=owner_id(conn))
    conn.close()

    resp = client.get("/")
    overdue_section = resp.text.split("Overdue</h2>")[1].split("<h2>Missed")[0]
    missed_section = resp.text.split("<h2>Missed")[1].split("Due today</h2>")[0]
    assert "Completed Long Ago" not in overdue_section
    assert "Completed Long Ago" not in missed_section


def test_cancelled_task_does_not_appear_in_overdue_or_missed(client):
    conn = connect(client.db_path)
    cancelled_id = _make_task(conn, external_id="cw-cancelled", title="Cancelled Assignment")
    conn.close()
    conn = connect(client.db_path)
    repo.cancel_task_from_source(conn, task_id=cancelled_id, user_id=owner_id(conn))
    conn.close()

    resp = client.get("/")
    overdue_section = resp.text.split("Overdue</h2>")[1].split("<h2>Missed")[0]
    missed_section = resp.text.split("<h2>Missed")[1].split("Due today</h2>")[0]
    assert "Cancelled Assignment" not in overdue_section
    assert "Cancelled Assignment" not in missed_section


def test_set_personal_deadline(client):
    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="material", external_id="mat-1",
        title="Lecture slides", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=None, source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get("/")
    assert "Lecture slides" in resp.text

    resp = client.post(
        f"/tasks/{result.task_id}/personal-deadline",
        data={"personal_deadline": "2026-08-30"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    conn = connect(client.db_path)
    row = conn.execute("SELECT personal_deadline, status FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    conn.close()
    assert row["personal_deadline"] == "2026-08-30"
