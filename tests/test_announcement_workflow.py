"""Tests for the announcement triage workflow: open -> create a personal
task -> ignore/archive. Fully deterministic; no AI is involved in any of
these paths, and the announcement itself is never modified.
"""

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.web.app import create_app


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "web.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path), follow_redirects=False)


def _announcement(conn, *, external_id="ann-1", title="Quiz on Friday") -> int:
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section=None, teacher=None,
        course_code="CS1004", state="ACTIVE",
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="announcement", external_id=external_id,
        title=title, description="Bring your notes.", link="http://classroom.example/a",
        kind="INFORMATIONAL", actual_deadline=None,
        source_published_at="2026-09-01T10:00:00Z", source_updated_at="2026-09-01T10:00:00Z",
    )
    return result.task_id


def test_creating_a_task_from_an_announcement_links_them(conn):
    announcement_id = _announcement(conn)

    task_id = repo.create_task_from_announcement(conn, announcement_task_id=announcement_id)

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["parent_task_id"] == announcement_id
    assert row["source_type"] == "manual"  # the created task is the user's own
    assert row["title"] == "Quiz on Friday"


def test_creating_twice_is_idempotent(conn):
    announcement_id = _announcement(conn)

    first = repo.create_task_from_announcement(conn, announcement_task_id=announcement_id)
    second = repo.create_task_from_announcement(conn, announcement_task_id=announcement_id)
    third = repo.create_task_from_announcement(conn, announcement_task_id=announcement_id)

    assert first == second == third
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE parent_task_id = ?", (announcement_id,)
    ).fetchone()["c"]
    assert count == 1


def test_the_announcement_itself_is_never_modified(conn):
    announcement_id = _announcement(conn)
    before = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (announcement_id,)).fetchone())

    repo.create_task_from_announcement(conn, announcement_task_id=announcement_id)

    after = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (announcement_id,)).fetchone())
    assert after == before


def test_created_task_can_carry_its_own_title_and_deadline(conn):
    announcement_id = _announcement(conn)

    task_id = repo.create_task_from_announcement(
        conn, announcement_task_id=announcement_id,
        title="Revise for Friday quiz", actual_deadline="2026-09-11T09:00:00+00:00",
    )

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["title"] == "Revise for Friday quiz"
    assert row["actual_deadline"] == "2026-09-11T09:00:00+00:00"


def test_non_announcement_tasks_are_rejected(conn):
    course_id = repo.upsert_course(
        conn, external_id="c1", name="OOP", section=None, teacher=None,
        course_code=None, state="ACTIVE",
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=None, source_published_at=None, source_updated_at=None,
    )

    with pytest.raises(ValueError):
        repo.create_task_from_announcement(conn, announcement_task_id=result.task_id)


def test_archiving_removes_an_announcement_from_triage(conn):
    announcement_id = _announcement(conn)
    assert len(repo.open_announcements(conn)) == 1

    repo.archive_task(conn, task_id=announcement_id)

    assert repo.open_announcements(conn) == []
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (announcement_id,)).fetchone()
    assert row["status"] == "ARCHIVED"


def test_archiving_is_idempotent_and_recorded(conn):
    announcement_id = _announcement(conn)
    repo.archive_task(conn, task_id=announcement_id)
    repo.archive_task(conn, task_id=announcement_id)

    history = conn.execute(
        "SELECT COUNT(*) AS c FROM task_history WHERE task_id = ? AND new_value = 'ARCHIVED'",
        (announcement_id,),
    ).fetchone()["c"]
    assert history == 1


def test_open_announcements_reports_whether_a_task_was_created(conn):
    announcement_id = _announcement(conn)
    assert repo.open_announcements(conn)[0]["child_task_id"] is None

    repo.create_task_from_announcement(conn, announcement_task_id=announcement_id)

    assert repo.open_announcements(conn)[0]["child_task_id"] is not None


# --- Routes ---------------------------------------------------------------


def test_announcement_route_creates_exactly_one_task_on_double_submit(client, db_path):
    with connect_closing(db_path) as conn:
        announcement_id = _announcement(conn)

    client.post(f"/announcements/{announcement_id}/create-task", data={"title": ""})
    client.post(f"/announcements/{announcement_id}/create-task", data={"title": ""})

    with connect_closing(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE parent_task_id = ?", (announcement_id,)
        ).fetchone()["c"]
    assert count == 1


def test_archive_route_archives(client, db_path):
    with connect_closing(db_path) as conn:
        announcement_id = _announcement(conn)

    response = client.post(f"/announcements/{announcement_id}/archive")

    assert response.status_code == 303
    with connect_closing(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (announcement_id,)
        ).fetchone()["status"]
    assert status == "ARCHIVED"


def test_announcements_page_lists_pending_items(client, db_path):
    with connect_closing(db_path) as conn:
        _announcement(conn, title="Guest lecture Monday")

    body = client.get("/announcements").text

    assert "Guest lecture Monday" in body


def test_invalid_deadline_from_the_announcement_form_is_rejected(client, db_path):
    with connect_closing(db_path) as conn:
        announcement_id = _announcement(conn)

    response = client.post(
        f"/announcements/{announcement_id}/create-task",
        data={"title": "x", "actual_deadline": "whenever"},
    )

    assert response.status_code == 400
    with connect_closing(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE parent_task_id = ?", (announcement_id,)
        ).fetchone()["c"]
    assert count == 0
