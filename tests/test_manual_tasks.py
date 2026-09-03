"""Tests for manual/personal tasks and the task-source write boundary.

The adversarial tests here are the security core of Phase 2. There is no
auth layer behind these routes, so the only thing standing between a
malformed POST and corrupted academic data is the guard being correct. Each
test posts fields the route does not declare and asserts every
Classroom-authoritative column is byte-identical afterward.

See docs/INTERFACES.md contract #5 for the field-ownership table these
tests enforce.
"""

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.web.app import create_app

from tests.support import owner_id

CLASSROOM_FIELDS = (
    "title", "description", "link", "actual_deadline", "kind",
    "course_id", "source_type", "external_id", "source_published_at", "source_updated_at",
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "web.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def client(db_path):
    c = TestClient(create_app(db_path), follow_redirects=False)
    return c


def _classroom_task(db_path) -> int:
    with connect_closing(db_path) as conn:
        course_id = repo.upsert_course(
            conn, external_id="course-1", name="OOP", section="BCS-3C",
            teacher=None, course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
        )
        result = repo.upsert_task_from_source(
            conn, course_id=course_id, source_type="coursework", external_id="cw-1",
            title="Real Classroom Assignment", description="from classroom", link="http://x",
            kind="ACTIONABLE", actual_deadline="2026-12-01T23:59:00+00:00",
            source_published_at="2026-08-01T00:00:00+00:00",
            source_updated_at="2026-08-01T00:00:00+00:00", user_id=owner_id(conn),
        )
        return result.task_id


def _snapshot(db_path, task_id) -> dict:
    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return {field: row[field] for field in CLASSROOM_FIELDS}


# --- Manual task lifecycle -------------------------------------------------


def test_create_edit_complete_and_cancel_a_manual_task(conn):
    task_id = repo.create_manual_task(conn, title="Write report", description="for me", user_id=owner_id(conn))
    assert task_id

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["source_type"] == "manual"
    assert row["external_id"] is None

    repo.update_manual_task(conn, task_id=task_id, title="Write final report", user_id=owner_id(conn))
    repo.set_personal_deadline(conn, task_id=task_id, personal_deadline="2026-09-20", user_id=owner_id(conn))
    repo.mark_completed(conn, task_id=task_id, user_id=owner_id(conn))
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"] == "COMPLETED"

    other = repo.create_manual_task(conn, title="Cancel me", user_id=owner_id(conn))
    repo.cancel_task(conn, task_id=other, user_id=owner_id(conn))
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (other,)).fetchone()["status"] == "CANCELLED"


def test_manual_task_requires_a_title(conn):
    with pytest.raises(ValueError):
        repo.create_manual_task(conn, title="   ", user_id=owner_id(conn))


def test_manual_task_belongs_to_the_personal_pseudo_course(conn):
    task_id = repo.create_manual_task(conn, title="Task", user_id=owner_id(conn))
    row = conn.execute(
        """SELECT courses.external_id FROM tasks JOIN courses ON courses.id = tasks.course_id
           WHERE tasks.id = ?""",
        (task_id,),
    ).fetchone()
    assert row["external_id"] == "__personal__"


def test_manual_task_edit_records_history(conn):
    task_id = repo.create_manual_task(conn, title="Before", user_id=owner_id(conn))
    repo.update_manual_task(conn, task_id=task_id, title="After", user_id=owner_id(conn))

    changes = conn.execute(
        "SELECT * FROM task_history WHERE task_id = ? AND field = 'title'", (task_id,)
    ).fetchall()
    assert changes[-1]["old_value"] == "Before"
    assert changes[-1]["new_value"] == "After"


# --- The boundary ----------------------------------------------------------


def test_editing_a_classroom_task_raises(conn):
    course_id = repo.upsert_course(
        conn, external_id="c1", name="OOP", section=None, teacher=None,
        course_code=None, state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Classroom Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=None, source_published_at=None, source_updated_at=None, user_id=owner_id(conn),
    )

    with pytest.raises(repo.TaskSourceViolation):
        repo.update_manual_task(conn, task_id=result.task_id, title="hijacked", user_id=owner_id(conn))


def test_cancelling_a_classroom_task_raises(conn):
    course_id = repo.upsert_course(
        conn, external_id="c1", name="OOP", section=None, teacher=None,
        course_code=None, state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Classroom Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=None, source_published_at=None, source_updated_at=None, user_id=owner_id(conn),
    )

    with pytest.raises(repo.TaskSourceViolation):
        repo.cancel_task(conn, task_id=result.task_id, user_id=owner_id(conn))


def test_personal_deadline_and_completion_remain_allowed_on_classroom_tasks(conn):
    # The amended contract #5: Ragra-owned fields stay editable on any task.
    # Removing this would delete a correct, shipped feature.
    course_id = repo.upsert_course(
        conn, external_id="c1", name="OOP", section=None, teacher=None,
        course_code=None, state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Classroom Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-12-01T23:59:00+00:00",
        source_published_at=None, source_updated_at=None, user_id=owner_id(conn),
    )

    repo.set_personal_deadline(conn, task_id=result.task_id, personal_deadline="2026-11-28", user_id=owner_id(conn))
    repo.mark_completed(conn, task_id=result.task_id, user_id=owner_id(conn))

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    assert row["personal_deadline"] == "2026-11-28"
    assert row["status"] == "COMPLETED"
    assert row["actual_deadline"] == "2026-12-01T23:59:00+00:00"  # untouched


def test_guard_rejects_a_nonexistent_task(conn):
    with pytest.raises(repo.TaskSourceViolation):
        repo.update_manual_task(conn, task_id=999999, title="ghost", user_id=owner_id(conn))


# --- Adversarial route tests ----------------------------------------------


def test_edit_route_refuses_a_classroom_task(client, db_path):
    task_id = _classroom_task(db_path)
    before = _snapshot(db_path, task_id)

    response = client.post(f"/tasks/{task_id}/edit", data={"title": "hijacked"})

    assert response.status_code == 403
    assert _snapshot(db_path, task_id) == before


def test_cancel_route_refuses_a_classroom_task(client, db_path):
    task_id = _classroom_task(db_path)

    response = client.post(f"/tasks/{task_id}/cancel")

    assert response.status_code == 403
    with connect_closing(db_path) as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
    assert status != "CANCELLED"


def test_extra_form_fields_cannot_reach_classroom_columns(client, db_path):
    # Mass-assignment attempt: post every Classroom-authoritative field to a
    # route that declares none of them.
    task_id = _classroom_task(db_path)
    before = _snapshot(db_path, task_id)

    client.post(
        f"/tasks/{task_id}/personal-deadline",
        data={
            "personal_deadline": "2026-11-01",
            "title": "HIJACKED",
            "description": "HIJACKED",
            "actual_deadline": "1999-01-01T00:00:00+00:00",
            "source_type": "manual",
            "external_id": "spoofed",
            "course_id": "999",
            "kind": "INFORMATIONAL",
            "link": "http://evil.example",
        },
    )

    assert _snapshot(db_path, task_id) == before  # nothing authoritative moved
    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT personal_deadline FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["personal_deadline"] == "2026-11-01"  # the one legitimate change applied


def test_create_route_cannot_forge_a_classroom_task(client, db_path):
    client.post(
        "/tasks/new",
        data={
            "title": "Mine",
            "source_type": "coursework",
            "external_id": "forged-classroom-id",
            "course_id": "1",
        },
    )

    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE title = 'Mine'").fetchone()
    # source_type/external_id are derived from context, never accepted.
    assert row["source_type"] == "manual"
    assert row["external_id"] is None


def test_manual_task_can_be_edited_through_the_route(client, db_path):
    with connect_closing(db_path) as conn:
        task_id = repo.create_manual_task(conn, title="Original", user_id=owner_id(conn))

    response = client.post(f"/tasks/{task_id}/edit", data={"title": "Updated"})

    assert response.status_code == 303
    with connect_closing(db_path) as conn:
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()["title"] == "Updated"


def test_invalid_deadline_is_rejected_rather_than_stored(client, db_path):
    response = client.post(
        "/tasks/new", data={"title": "Bad date", "actual_deadline": "not-a-date"}
    )

    assert response.status_code == 400
    with connect_closing(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0


def test_empty_deadline_means_none_not_now(client, db_path):
    client.post("/tasks/new", data={"title": "No deadline", "actual_deadline": ""})

    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE title = 'No deadline'").fetchone()
    assert row["actual_deadline"] is None


def test_tasks_page_lists_only_manual_tasks(client, db_path):
    _classroom_task(db_path)
    with connect_closing(db_path) as conn:
        repo.create_manual_task(conn, title="My Own Task", user_id=owner_id(conn))

    body = client.get("/tasks").text

    assert "My Own Task" in body
    assert "Real Classroom Assignment" not in body
