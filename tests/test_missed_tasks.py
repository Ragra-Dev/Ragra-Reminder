"""Tests for missed-task reconciliation (repo.mark_overdue_tasks_as_missed),
wired into sync_classroom - no new state model, reusing the existing
mark_missed function that already existed but was never called anywhere.
"""

from datetime import datetime, timedelta, timezone

from ragra.db import repo
from ragra.sync.classroom_sync import sync_classroom
from tests.test_classroom_sync import COURSE, FakeClient

from tests.support import owner_id


def _make_course(conn):
    return repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )


def test_overdue_task_transitions_to_missed(conn):
    course_id = _make_course(conn)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Overdue Thing", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=past, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )

    missed = repo.mark_overdue_tasks_as_missed(conn, now=datetime.now(timezone.utc).isoformat(), user_id=owner_id(conn))

    assert missed == [result.task_id]
    row = conn.execute("SELECT status, missed_at FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    assert row["status"] == "MISSED"
    assert row["missed_at"] is not None

    history = conn.execute(
        "SELECT * FROM task_history WHERE task_id = ? AND new_value = 'MISSED'", (result.task_id,)
    ).fetchall()
    assert len(history) == 1


def test_overdue_task_transition_cancels_its_pending_reminders(conn):
    # A task that only just became overdue this tick may still carry a
    # PENDING pre-deadline reminder (e.g. a FINAL_1H reminder that never
    # got a chance to fire) - transitioning it to MISSED must cancel that
    # reminder too, so it never fires a now-nonsensical "due in 1 hour"
    # notification for a deadline that has already passed.
    course_id = _make_course(conn)
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-stray",
        title="Just Missed It", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=past, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    repo.insert_reminder_if_absent(
        conn, task_id=result.task_id, reminder_type="FINAL_1H",
        scheduled_for=past, idempotency_key=f"{result.task_id}:FINAL_1H:v1", user_id=owner_id(conn),
    )
    pending_before = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE task_id = ? AND status = 'PENDING'", (result.task_id,)
    ).fetchone()["c"]
    assert pending_before == 1

    repo.mark_overdue_tasks_as_missed(conn, now=datetime.now(timezone.utc).isoformat(), user_id=owner_id(conn))

    reminder = conn.execute(
        "SELECT status FROM reminders WHERE task_id = ? AND reminder_type = 'FINAL_1H'", (result.task_id,)
    ).fetchone()
    assert reminder["status"] == "CANCELLED"


def test_completed_task_is_not_marked_missed(conn):
    course_id = _make_course(conn)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-2",
        title="Completed On Time", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=past, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    repo.mark_completed(conn, task_id=result.task_id, user_id=owner_id(conn))

    missed = repo.mark_overdue_tasks_as_missed(conn, now=datetime.now(timezone.utc).isoformat(), user_id=owner_id(conn))

    assert missed == []
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    assert row["status"] == "COMPLETED"


def test_cancelled_task_is_not_marked_missed(conn):
    course_id = _make_course(conn)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-3",
        title="Cancelled Assignment", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=past, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    repo.cancel_task_from_source(conn, task_id=result.task_id, user_id=owner_id(conn))

    missed = repo.mark_overdue_tasks_as_missed(conn, now=datetime.now(timezone.utc).isoformat(), user_id=owner_id(conn))

    assert missed == []
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    assert row["status"] == "CANCELLED"


def test_future_task_is_not_marked_missed(conn):
    course_id = _make_course(conn)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-4",
        title="Not Due Yet", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=future, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )

    missed = repo.mark_overdue_tasks_as_missed(conn, now=datetime.now(timezone.utc).isoformat(), user_id=owner_id(conn))

    assert missed == []
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    assert row["status"] != "MISSED"


def test_repeated_reconciliation_is_idempotent(conn):
    course_id = _make_course(conn)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-5",
        title="Overdue Thing", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=past, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )

    now = datetime.now(timezone.utc).isoformat()
    first = repo.mark_overdue_tasks_as_missed(conn, now=now, user_id=owner_id(conn))
    row1 = conn.execute("SELECT missed_at FROM tasks WHERE id = ?", (result.task_id,)).fetchone()

    second = repo.mark_overdue_tasks_as_missed(conn, now=now, user_id=owner_id(conn))
    third = repo.mark_overdue_tasks_as_missed(conn, now=now, user_id=owner_id(conn))
    row2 = conn.execute("SELECT missed_at FROM tasks WHERE id = ?", (result.task_id,)).fetchone()

    assert first == [result.task_id]
    assert second == []  # already MISSED - not re-selected
    assert third == []
    assert row1["missed_at"] == row2["missed_at"]  # never re-touched

    history = conn.execute(
        "SELECT COUNT(*) AS c FROM task_history WHERE task_id = ? AND new_value = 'MISSED'", (result.task_id,)
    ).fetchone()
    assert history["c"] == 1  # exactly one history entry, not one per call


def test_missed_tasks_ordered_most_recent_deadline_first(conn):
    course_id = _make_course(conn)
    now = datetime.now(timezone.utc)

    def _missed(external_id, title, days_ago):
        deadline = (now - timedelta(days=days_ago)).isoformat()
        result = repo.upsert_task_from_source(
            conn, course_id=course_id, source_type="coursework", external_id=external_id,
            title=title, description=None, link=None, kind="ACTIONABLE",
            actual_deadline=deadline, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
        )
        repo.mark_missed(conn, task_id=result.task_id, user_id=owner_id(conn))
        return result.task_id

    old_id = _missed("cw-old", "Old Historical Thing", 150)
    recent_id = _missed("cw-recent", "Recently Missed Thing", 2)
    mid_id = _missed("cw-mid", "Medium Age Thing", 30)

    rows = repo.missed_tasks(conn, user_id=owner_id(conn))
    assert [r["id"] for r in rows] == [recent_id, mid_id, old_id]


def test_missed_tasks_limit_returns_only_the_most_recent(conn):
    course_id = _make_course(conn)
    now = datetime.now(timezone.utc)
    ids = []
    for i, days_ago in enumerate([100, 5, 50, 1, 20]):
        deadline = (now - timedelta(days=days_ago)).isoformat()
        result = repo.upsert_task_from_source(
            conn, course_id=course_id, source_type="coursework", external_id=f"cw-{i}",
            title=f"Task {i}", description=None, link=None, kind="ACTIONABLE",
            actual_deadline=deadline, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
        )
        repo.mark_missed(conn, task_id=result.task_id, user_id=owner_id(conn))
        ids.append((result.task_id, days_ago))

    # Sorted by days_ago ascending = most recent deadline first.
    expected_top2 = [tid for tid, _ in sorted(ids, key=lambda pair: pair[1])[:2]]

    limited = repo.missed_tasks(conn, limit=2, user_id=owner_id(conn))
    assert [r["id"] for r in limited] == expected_top2
    assert repo.count_missed_tasks(conn, user_id=owner_id(conn)) == 5


def test_wired_into_real_sync_flow(conn):
    """End-to-end: sync_classroom itself (not just the repo function
    directly) marks a genuinely overdue task missed, and running the sync
    repeatedly stays idempotent."""
    past = (datetime.now(timezone.utc) - timedelta(days=3))
    assignment = {
        "id": "cw-real",
        "title": "Ancient Homework",
        "description": "desc",
        "alternateLink": None,
        "creationTime": "2020-01-01T00:00:00Z",
        "updateTime": "2020-01-01T00:00:00Z",
        "dueDate": {"year": past.year, "month": past.month, "day": past.day},
        "dueTime": {"hours": 23, "minutes": 59},
    }
    client = FakeClient([COURSE], coursework={"course-1": [assignment]})

    summary1 = sync_classroom(conn, client, user_id=owner_id(conn))
    summary2 = sync_classroom(conn, client, user_id=owner_id(conn))

    assert summary1.tasks_marked_missed == 1
    assert summary2.tasks_marked_missed == 0  # idempotent on repeated tick/sync

    task = conn.execute("SELECT status FROM tasks WHERE external_id = 'cw-real'").fetchone()
    assert task["status"] == "MISSED"


def test_cancel_stray_reminders_for_terminal_tasks_cleans_up_each_terminal_status(conn):
    """Safety net (same style as cancel_backlog_reminders_for_already_overdue_tasks):
    a task that is already COMPLETED, CANCELLED, or MISSED must never carry
    a PENDING reminder, regardless of how it got left behind. Idempotent,
    only ever cancels PENDING rows, and never touches a legitimately-current
    task's reminder."""
    course_id = _make_course(conn)
    now = datetime.now(timezone.utc)

    def _task_with_pending_reminder(external_id, title):
        result = repo.upsert_task_from_source(
            conn, course_id=course_id, source_type="coursework", external_id=external_id,
            title=title, description=None, link=None, kind="ACTIONABLE",
            actual_deadline=(now + timedelta(days=5)).isoformat(),
            source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
        )
        repo.insert_reminder_if_absent(
            conn, task_id=result.task_id, reminder_type="T_MINUS_1D",
            scheduled_for=(now + timedelta(days=4)).isoformat(),
            idempotency_key=f"{result.task_id}:T_MINUS_1D:v1", user_id=owner_id(conn),
        )
        return result.task_id

    completed_id = _task_with_pending_reminder("cw-completed", "Will Complete")
    cancelled_id = _task_with_pending_reminder("cw-cancelled", "Will Cancel")
    missed_id = _task_with_pending_reminder("cw-missed", "Will Miss")
    current_id = _task_with_pending_reminder("cw-current", "Still Current")

    # Simulate stray PENDING rows left behind by data written before a given
    # call site paired itself with cancel_pending_reminders - the state
    # transition happens WITHOUT also cancelling the reminder here.
    conn.execute("UPDATE tasks SET status = 'COMPLETED' WHERE id = ?", (completed_id,))
    conn.execute("UPDATE tasks SET status = 'CANCELLED' WHERE id = ?", (cancelled_id,))
    conn.execute("UPDATE tasks SET status = 'MISSED' WHERE id = ?", (missed_id,))
    conn.commit()

    cancelled_count = repo.cancel_stray_reminders_for_terminal_tasks(conn, user_id=owner_id(conn))
    assert cancelled_count == 3

    def _status(task_id):
        return conn.execute(
            "SELECT status FROM reminders WHERE task_id = ?", (task_id,)
        ).fetchone()["status"]

    assert _status(completed_id) == "CANCELLED"
    assert _status(cancelled_id) == "CANCELLED"
    assert _status(missed_id) == "CANCELLED"
    assert _status(current_id) == "PENDING"  # legitimately-current task untouched

    # Idempotent: nothing left to cancel on a second pass.
    assert repo.cancel_stray_reminders_for_terminal_tasks(conn, user_id=owner_id(conn)) == 0
