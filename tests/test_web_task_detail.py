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
    assert "Fri 11 Sep" in resp.text or "Thu 10 Sep" in resp.text  # deadline, as a readable campus-time date
    assert "2026-09-10T23:59:00+00:00" not in resp.text            # no raw timestamps on the page
    assert "Tue 8 Sep" in resp.text  # personal target, as a readable date
    assert "Planned" in resp.text  # status, in plain words (it has a personal target, so it is planned)
    assert ">PLANNED<" not in resp.text


def test_task_detail_shows_description_and_classroom_link(client):
    resp = client.get(f"/tasks/{client.task_id}")
    assert "Implement a linked list." in resp.text
    assert "https://classroom.google.com/c/x/a/y" in resp.text


def test_task_detail_shows_reminder_state(client):
    resp = client.get(f"/tasks/{client.task_id}")
    assert "1 day before" in resp.text            # plain name, not T_MINUS_1D
    assert "Scheduled" in resp.text               # plain status, not PENDING
    assert "T_MINUS_1D" not in resp.text and "PENDING" not in resp.text


def test_task_detail_shows_history(client):
    resp = client.get(f"/tasks/{client.task_id}")
    assert "What changed" in resp.text and "Your own target" in resp.text   # plain label, not personal_deadline
    assert "personal_deadline" not in resp.text


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
    assert "No due date" in resp.text


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

    # the home page's upcoming-deadline card opens the detail page in the Deadlines section
    resp = client.get("/")
    assert f'href="/deadlines/{result.task_id}"' in resp.text
    assert client.get(f"/tasks/{result.task_id}").status_code == 200       # the full technical view still works


def test_no_sidebar_tab_is_highlighted_on_a_single_task_page(client):
    detail = client.get(f"/tasks/{client.task_id}").text
    assert 'class="on"' not in detail          # not the sidebar, not the phone bottom bar
    assert 'href="/tasks" class="on"' not in detail
    assert 'href="/tasks" class="on"' in client.get("/tasks").text         # the Tasks list itself still highlights Tasks
