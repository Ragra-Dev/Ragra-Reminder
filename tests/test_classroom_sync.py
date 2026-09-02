import pytest

from ragra.sync.classroom_sync import sync_classroom


class FakeClient:
    def __init__(self, courses, coursework=None, announcements=None, materials=None):
        self._courses = courses
        self._coursework = coursework or {}
        self._announcements = announcements or {}
        self._materials = materials or {}

    def list_courses(self):
        return self._courses

    def list_course_work(self, course_id):
        return self._coursework.get(course_id, [])

    def list_announcements(self, course_id):
        return self._announcements.get(course_id, [])

    def list_course_materials(self, course_id):
        return self._materials.get(course_id, [])


COURSE = {"id": "course-1", "name": "OOP", "section": "BCS-3C", "courseState": "ACTIVE"}

ASSIGNMENT_V1 = {
    "id": "cw-1",
    "title": "Assignment 2",
    "description": "desc",
    "alternateLink": "https://classroom.google.com/x",
    "creationTime": "2026-08-20T00:00:00Z",
    "updateTime": "2026-08-20T00:00:00Z",
    "dueDate": {"year": 2026, "month": 9, "day": 10},
    "dueTime": {"hours": 23, "minutes": 59},
}


def test_repeated_full_sync_does_not_duplicate_courses_or_tasks(conn):
    client = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})

    summary1 = sync_classroom(conn, client)
    summary2 = sync_classroom(conn, client)
    summary3 = sync_classroom(conn, client)

    assert summary1.tasks_created == 1
    assert summary2.tasks_created == 0
    assert summary3.tasks_created == 0

    # Excludes the seeded '__personal__' pseudo-course, which always exists
    # and holds manual tasks rather than anything Classroom syncs.
    course_count = conn.execute(
        "SELECT COUNT(*) AS c FROM courses WHERE external_id != '__personal__'"
    ).fetchone()["c"]
    task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert course_count == 1
    assert task_count == 1


def test_sync_schedules_reminders_for_new_actionable_task(conn):
    client = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    sync_classroom(conn, client)

    reminders = conn.execute("SELECT * FROM reminders").fetchall()
    assert len(reminders) > 0


def test_deadline_change_updates_task_and_recomputes_reminders(conn):
    client_v1 = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    sync_classroom(conn, client_v1)

    reminders_before = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE status = 'PENDING'"
    ).fetchone()["c"]
    assert reminders_before > 0

    assignment_v2 = dict(ASSIGNMENT_V1)
    assignment_v2["dueDate"] = {"year": 2026, "month": 9, "day": 13}
    assignment_v2["updateTime"] = "2026-08-25T00:00:00Z"
    client_v2 = FakeClient([COURSE], coursework={"course-1": [assignment_v2]})

    summary = sync_classroom(conn, client_v2)
    assert len(summary.deadlines_changed) == 1

    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-1'").fetchone()
    assert task["actual_deadline"].startswith("2026-09-13")

    # Old reminders cancelled, new ones pending - never both active for the
    # same deadline.
    cancelled = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE status = 'CANCELLED'"
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE status = 'PENDING'"
    ).fetchone()["c"]
    assert cancelled > 0
    assert pending > 0

    still_one_task = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert still_one_task == 1


def test_archived_course_is_skipped(conn):
    archived = dict(COURSE)
    archived["courseState"] = "ARCHIVED"
    client = FakeClient([archived], coursework={"course-1": [ASSIGNMENT_V1]})
    summary = sync_classroom(conn, client)
    assert summary.courses_seen == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0


@pytest.mark.parametrize(
    "non_eligible_state",
    [
        "ARCHIVED",
        "DECLINED",
        "SUSPENDED",
        "COURSE_STATE_UNSPECIFIED",
        # HIDDEN is not a real Classroom courseState value - verified
        # directly against the official Classroom API v1 discovery document
        # (schemas.Course.properties.courseState.enum), which lists exactly
        # ACTIVE/ARCHIVED/PROVISIONED/DECLINED/SUSPENDED/
        # COURSE_STATE_UNSPECIFIED and nothing else. The student-facing
        # "hide this class" toggle in Classroom's own UI is a client-side
        # preference the API never returns, so there's nothing to filter on
        # for it specifically. This value is included anyway as a
        # forward-compatible check: the sync loop's ACTIVE/PROVISIONED
        # allow-list (see classroom_sync.py) excludes ANY state it doesn't
        # explicitly recognize, so it would already correctly exclude a
        # "HIDDEN" state too, with zero code change, if Google ever adds one.
        "HIDDEN",
    ],
)
def test_non_active_or_provisioned_course_states_never_generate_reminders(conn, non_eligible_state):
    from ragra.db import repo

    course = dict(COURSE)
    course["courseState"] = non_eligible_state
    client = FakeClient([course], coursework={"course-1": [ASSIGNMENT_V1]})

    summary = sync_classroom(conn, client)

    assert summary.courses_seen == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0
    due = repo.due_pending_reminders(conn, now="2026-12-31T00:00:00+00:00")
    assert due == []


def test_previously_active_course_going_archived_updates_stored_state(conn):
    # Distinct from test_archived_course_is_skipped: this course was ACTIVE
    # on a prior sync (with a real task and reminder already created), and
    # only goes ARCHIVED on a later sync - the stored course state must
    # reflect that so due_pending_reminders can exclude its tasks, and no
    # existing history should be deleted.
    client_active = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    sync_classroom(conn, client_active)

    course_row = conn.execute("SELECT state FROM courses WHERE external_id = 'course-1'").fetchone()
    assert course_row["state"] == "ACTIVE"
    task_count_before = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert task_count_before == 1

    archived = dict(COURSE)
    archived["courseState"] = "ARCHIVED"
    client_archived = FakeClient([archived], coursework={"course-1": [ASSIGNMENT_V1]})
    summary = sync_classroom(conn, client_archived)

    assert summary.courses_seen == 0
    course_row_after = conn.execute("SELECT state FROM courses WHERE external_id = 'course-1'").fetchone()
    assert course_row_after["state"] == "ARCHIVED"
    # History preserved - the task row is untouched, not deleted.
    assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == task_count_before

    from ragra.db import repo

    due = repo.due_pending_reminders(conn, now="2026-12-31T00:00:00+00:00")
    assert due == []  # the archived course's task must not generate a normal reminder


def test_course_group_email_is_never_used_as_course_code(conn):
    course_with_group_email = dict(COURSE)
    course_with_group_email["courseGroupEmail"] = "course_alias_example@example.com"
    client = FakeClient([course_with_group_email])
    sync_classroom(conn, client)

    row = conn.execute("SELECT course_code, name FROM courses WHERE external_id = 'course-1'").fetchone()
    assert row["course_code"] is None
    assert "@" not in (row["course_code"] or "")


def test_due_pending_reminders_falls_back_to_course_name_when_no_code(conn):
    from ragra.db import repo

    client = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    sync_classroom(conn, client)

    due = repo.due_pending_reminders(conn, now="2026-12-31T00:00:00+00:00")
    assert due
    assert due[0]["course_code"] == "OOP"  # falls back to the real course name


def test_sync_never_overwrites_personal_deadline(conn):
    from ragra.db import repo

    client_v1 = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    sync_classroom(conn, client_v1)
    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-1'").fetchone()

    repo.set_personal_deadline(conn, task_id=task["id"], personal_deadline="2026-09-05")

    # Re-sync with no change: routine repeated sync must not touch it.
    sync_classroom(conn, client_v1)
    unchanged = conn.execute("SELECT personal_deadline FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    assert unchanged["personal_deadline"] == "2026-09-05"

    # Re-sync with the ACTUAL deadline changed at the source: the personal
    # target must still be untouched - they are independent properties.
    assignment_v2 = dict(ASSIGNMENT_V1)
    assignment_v2["dueDate"] = {"year": 2026, "month": 9, "day": 13}
    assignment_v2["updateTime"] = "2026-08-25T00:00:00Z"
    client_v2 = FakeClient([COURSE], coursework={"course-1": [assignment_v2]})
    sync_classroom(conn, client_v2)

    row = conn.execute("SELECT actual_deadline, personal_deadline FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    assert row["actual_deadline"].startswith("2026-09-13")
    assert row["personal_deadline"] == "2026-09-05"


def test_deleted_coursework_cancels_the_task_and_its_reminders(conn):
    client_v1 = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    sync_classroom(conn, client_v1)

    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-1'").fetchone()
    assert task["status"] != "CANCELLED"
    pending_before = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE task_id = ? AND status = 'PENDING'", (task["id"],)
    ).fetchone()["c"]
    assert pending_before > 0

    # Next sync: the assignment no longer comes back from Classroom (deleted
    # or unpublished at the source).
    client_v2 = FakeClient([COURSE], coursework={"course-1": []})
    summary = sync_classroom(conn, client_v2)

    assert summary.tasks_cancelled == 1
    task_after = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-1'").fetchone()
    assert task_after["status"] == "CANCELLED"
    pending_after = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE task_id = ? AND status = 'PENDING'", (task["id"],)
    ).fetchone()["c"]
    assert pending_after == 0

    # Still exactly one task row - cancellation is a status change, not a
    # delete, so history is preserved.
    assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1


def test_completed_task_is_not_reopened_or_touched_if_source_item_disappears(conn):
    client_v1 = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    sync_classroom(conn, client_v1)
    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-1'").fetchone()

    from ragra.db import repo
    repo.mark_completed(conn, task_id=task["id"])

    client_v2 = FakeClient([COURSE], coursework={"course-1": []})
    summary = sync_classroom(conn, client_v2)

    assert summary.tasks_cancelled == 0
    task_after = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-1'").fetchone()
    assert task_after["status"] == "COMPLETED"


def test_sync_never_attempts_a_teacher_roster_lookup(conn):
    # FakeClient deliberately has no list_teachers method at all. If
    # sync_classroom ever called it again, this would raise AttributeError,
    # get caught by sync_classroom's top-level handler, and show up as a
    # sync error - which the assertions below would catch.
    client = FakeClient([COURSE], coursework={"course-1": [ASSIGNMENT_V1]})
    summary = sync_classroom(conn, client)

    assert summary.errors == []
    assert summary.courses_seen == 1
    assert not hasattr(summary, "teacher_lookups_skipped")

    course = conn.execute("SELECT teacher FROM courses WHERE external_id = 'course-1'").fetchone()
    assert course["teacher"] is None


def test_repeated_sync_with_no_roster_scope_produces_no_errors_across_many_courses(conn):
    courses = [
        {"id": f"course-{i}", "name": f"Course {i}", "section": "S", "courseState": "ACTIVE"}
        for i in range(5)
    ]
    client = FakeClient(courses)

    for _ in range(3):
        summary = sync_classroom(conn, client)
        assert summary.errors == []
        assert summary.courses_seen == 5


def test_announcement_without_deadline_never_gets_invented_deadline(conn):
    announcement = {
        "id": "ann-1",
        "text": "Midterm covers chapters 1-5",
        "creationTime": "2026-08-20T00:00:00Z",
        "updateTime": "2026-08-20T00:00:00Z",
    }
    client = FakeClient([COURSE], announcements={"course-1": [announcement]})
    sync_classroom(conn, client)

    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'ann-1'").fetchone()
    assert task["actual_deadline"] is None
    assert task["kind"] == "INFORMATIONAL"
