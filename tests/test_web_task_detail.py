from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect
from ragra.web.app import create_app

from tests.support import owner_id, sign_in


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "detail-test.db"
    conn = connect(db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="Object Oriented Programming", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment 2", description="Implement a linked list.",
        link="https://classroom.google.com/c/x/a/y",
        kind="ACTIONABLE", actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    repo.set_personal_deadline(conn, task_id=result.task_id, personal_deadline="2026-09-08", user_id=owner_id(conn))
    repo.insert_reminder_if_absent(
        conn, task_id=result.task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T23:59:00+00:00", idempotency_key="k1", user_id=owner_id(conn),
    )
    conn.close()

    app = create_app(db_path)
    with TestClient(app) as c:
        sign_in(c, db_path)
        c.task_id = result.task_id  # type: ignore[attr-defined]
        c.db_path = db_path  # type: ignore[attr-defined]
        yield c


def test_task_detail_shows_core_fields(client):
    resp = client.get(f"/tasks/{client.task_id}")
    assert resp.status_code == 200
    assert "Assignment 2" in resp.text
    assert "Object Oriented Programming" in resp.text
    assert "2026-09-10T23:59:00+00:00" in resp.text  # actual deadline
    assert "2026-09-08" in resp.text  # personal deadline
    assert "ACTION_REQUIRED" in resp.text  # status


def test_task_detail_shows_description_and_classroom_link(client):
    resp = client.get(f"/tasks/{client.task_id}")
    assert "Implement a linked list." in resp.text
    assert "https://classroom.google.com/c/x/a/y" in resp.text


def test_task_detail_shows_reminder_state(client):
    resp = client.get(f"/tasks/{client.task_id}")
    assert "T_MINUS_1D" in resp.text
    assert "PENDING" in resp.text
    assert "2026-09-09T23:59:00+00:00" in resp.text


def test_task_detail_shows_history(client):
    resp = client.get(f"/tasks/{client.task_id}")
    assert "personal_deadline" in resp.text


def test_task_detail_404_for_missing_task(client):
    resp = client.get("/tasks/999999")
    assert resp.status_code == 404


def test_task_without_description_or_link_renders_fine(client):
    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="Object Oriented Programming", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="material", external_id="mat-1",
        title="Bare Task", description=None, link=None, kind="INFORMATIONAL",
        actual_deadline=None, source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get(f"/tasks/{result.task_id}")
    assert resp.status_code == 200
    assert "Bare Task" in resp.text
    assert "No academic deadline set" in resp.text
    assert "Not set" in resp.text  # personal deadline
    assert "No reminders recorded" in resp.text


def test_dashboard_task_titles_link_to_detail_view(client):
    from datetime import datetime, timedelta, timezone

    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="Object Oriented Programming", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-soon",
        title="Due Soon Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get("/")
    assert f'href="/tasks/{result.task_id}"' in resp.text
