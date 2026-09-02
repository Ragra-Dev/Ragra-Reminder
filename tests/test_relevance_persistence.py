"""Tests for relevance persistence and its wiring into Classroom sync.

The point of these tests is not that a decision gets stored - it is that
storing a decision can never hide, drop, or suppress a task. Phase 1 proved
fail-open inside the pure engine; these prove it survives the trip through
sync and into the database, which is where a fail-closed regression would
actually cost the user an assignment.
"""

from ragra.db import repo
from ragra.relevance.engine import RelevanceDecision
from ragra.sync.classroom_sync import sync_classroom


class FakeClassroomClient:
    """Minimal ClassroomClient double: one course, controllable coursework."""

    def __init__(self, course_name="DLD LAB FALL'26", coursework=None):
        self._course_name = course_name
        self._coursework = coursework or []

    def list_courses(self):
        return [{"id": "course-1", "name": self._course_name, "courseState": "ACTIVE"}]

    def list_course_work(self, course_id):
        return self._coursework

    def list_announcements(self, course_id):
        return []

    def list_course_materials(self, course_id):
        return []


def _item(external_id, title, description=None, updated="2026-09-01T00:00:00Z"):
    return {
        "id": external_id,
        "title": title,
        "description": description,
        "creationTime": "2026-08-01T00:00:00Z",
        "updateTime": updated,
    }


def _task(conn, external_id):
    return conn.execute("SELECT * FROM tasks WHERE external_id = ?", (external_id,)).fetchone()


def test_relevance_columns_default_to_relevant(conn):
    # Migration 0002's default is the fail-open guarantee: applying it
    # cannot hide a single already-synced task.
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(tasks)")}
    assert columns["relevance"]["dflt_value"] == "'RELEVANT'"
    assert columns["relevance"]["notnull"] == 1


def test_matching_section_is_stored_as_relevant(conn):
    client = FakeClassroomClient(
        course_name="OOP Theory", coursework=[_item("cw-1", "Lab 02 Section C")]
    )
    summary = sync_classroom(conn, client)

    assert summary.errors == []
    assert _task(conn, "cw-1")["relevance"] == RelevanceDecision.RELEVANT.value


def test_other_section_is_stored_but_the_task_is_still_present_and_visible(conn):
    # The whole fail-open contract in one test: a different section may
    # suppress a *notification*, never the task itself.
    item = _item("cw-other", "Lab 02 Section E")
    item["dueDate"] = {"year": 2026, "month": 12, "day": 1}
    item["dueTime"] = {"hours": 23, "minutes": 59}
    client = FakeClassroomClient(course_name="OOP Theory", coursework=[item])
    sync_classroom(conn, client)

    task = _task(conn, "cw-other")
    assert task["relevance"] == RelevanceDecision.OTHER_SECTION.value
    assert task["status"] != "CANCELLED"
    assert task["relevance_reason"]  # explains itself rather than silently hiding

    # Still returned by the ordinary listing paths - not filtered out anywhere.
    assert any(r["id"] == task["id"] for r in repo.tasks_missing_personal_target(conn))


def test_ambiguous_content_is_stored_as_unknown_and_never_suppressed(conn):
    client = FakeClassroomClient(
        course_name="OOP Theory", coursework=[_item("cw-amb", "Sections A-D")]
    )
    sync_classroom(conn, client)

    task = _task(conn, "cw-amb")
    assert task["relevance"] == RelevanceDecision.UNKNOWN.value
    assert task["relevance"] != RelevanceDecision.OTHER_SECTION.value


def test_all_sections_announcement_is_relevant(conn):
    client = FakeClassroomClient(
        course_name="OOP Theory", coursework=[_item("cw-all", "Quiz for all sections")]
    )
    sync_classroom(conn, client)

    assert _task(conn, "cw-all")["relevance"] == RelevanceDecision.RELEVANT.value


def test_resync_does_not_churn_the_stored_decision(conn):
    client = FakeClassroomClient(
        course_name="OOP Theory", coursework=[_item("cw-1", "Lab 02 Section C")]
    )
    sync_classroom(conn, client)
    first = _task(conn, "cw-1")["relevance_computed_at"]
    history_before = conn.execute(
        "SELECT COUNT(*) AS c FROM task_history WHERE field = 'relevance'"
    ).fetchone()["c"]

    sync_classroom(conn, client)
    sync_classroom(conn, client)

    assert _task(conn, "cw-1")["relevance_computed_at"] == first  # not rewritten
    history_after = conn.execute(
        "SELECT COUNT(*) AS c FROM task_history WHERE field = 'relevance'"
    ).fetchone()["c"]
    assert history_after == history_before  # no history flooding


def test_relevance_change_is_recorded_in_history(conn):
    client = FakeClassroomClient(
        course_name="OOP Theory", coursework=[_item("cw-1", "Lab 02 Section C")]
    )
    sync_classroom(conn, client)

    # The teacher retitles the item to another section.
    client._coursework = [_item("cw-1", "Lab 02 Section E", updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client)

    assert _task(conn, "cw-1")["relevance"] == RelevanceDecision.OTHER_SECTION.value
    changes = conn.execute(
        "SELECT * FROM task_history WHERE field = 'relevance' ORDER BY id"
    ).fetchall()
    assert changes[-1]["old_value"] == RelevanceDecision.RELEVANT.value
    assert changes[-1]["new_value"] == RelevanceDecision.OTHER_SECTION.value


def test_relevance_failure_never_fails_the_sync_or_loses_the_task(conn, monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("relevance engine blew up")

    monkeypatch.setattr("ragra.sync.classroom_sync.is_relevant", _explode)
    client = FakeClassroomClient(coursework=[_item("cw-boom", "Lab 02 Section C")])

    summary = sync_classroom(conn, client)

    assert summary.errors == []  # the academic sync itself succeeded
    assert summary.warnings  # but the problem is surfaced, not swallowed silently
    task = _task(conn, "cw-boom")
    assert task is not None
    assert task["relevance"] == "RELEVANT"  # fail-open default retained


def test_unavailable_profile_disables_relevance_without_breaking_sync(conn, monkeypatch):
    def _explode():
        raise RuntimeError("profile unavailable")

    monkeypatch.setattr("ragra.sync.classroom_sync.load_profile", _explode)
    client = FakeClassroomClient(coursework=[_item("cw-noprofile", "Lab 02 Section E")])

    summary = sync_classroom(conn, client)

    assert summary.errors == []
    assert summary.warnings
    # Would otherwise have been OTHER_SECTION; with no profile it stays open.
    assert _task(conn, "cw-noprofile")["relevance"] == "RELEVANT"
