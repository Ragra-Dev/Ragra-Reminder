"""Tests for the historical-backlog suppression policy.

Policy: a task whose actual_deadline was already at-or-before the moment
Ragra itself first discovered it (its own created_at, NOT Classroom's
creationTime) never gets pre-deadline reminders. A task discovered with
real time left before its deadline behaves normally. This distinguishes
"first-ever import of old Classroom history" from "genuinely new work"
without any AI, without touching actual_deadline/personal_deadline
semantics, and without deleting any existing reminder row (only
cancelling PENDING ones).
"""

from datetime import datetime, timedelta, timezone

from ragra.db import repo
from ragra.sync.classroom_sync import sync_classroom
from tests.test_classroom_sync import COURSE, FakeClient


def _due_at(dt: datetime) -> dict:
    return {"year": dt.year, "month": dt.month, "day": dt.day}


def _due_time_at(dt: datetime) -> dict:
    return {"hours": dt.hour, "minutes": dt.minute}


def _assignment(cw_id: str, title: str, due_dt: datetime) -> dict:
    return {
        "id": cw_id,
        "title": title,
        "description": "desc",
        "alternateLink": "https://classroom.google.com/x",
        "creationTime": "2020-01-01T00:00:00Z",  # deliberately ancient/irrelevant
        "updateTime": "2020-01-01T00:00:00Z",
        "dueDate": _due_at(due_dt),
        "dueTime": _due_time_at(due_dt),
    }


def test_historical_overdue_assignment_imported_first_time_gets_no_reminders(conn):
    long_past_due = datetime.now(timezone.utc) - timedelta(days=200)
    client = FakeClient([COURSE], coursework={"course-1": [_assignment("cw-hist", "Old Homework", long_past_due)]})

    sync_classroom(conn, client)

    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-hist'").fetchone()
    reminders = conn.execute("SELECT * FROM reminders WHERE task_id = ?", (task["id"],)).fetchall()
    assert reminders == []
    # The reminder-backlog policy suppresses reminders, not the fact that
    # the work is overdue: 200 days past deadline, the missed-task
    # reconciliation (wired into sync_classroom) correctly transitions it
    # to MISSED - a more precise state than plain "overdue".
    task_after = conn.execute("SELECT status, missed_at FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    assert task_after["status"] == "MISSED"
    assert task_after["missed_at"] is not None


def test_newly_discovered_overdue_assignment_behaves_normally_ie_no_backlog_flood(conn):
    """An assignment that happens to already be overdue at the moment of
    discovery (regardless of WHY) still gets zero pre-deadline reminders -
    that has always been the deterministic rule (you cannot remind someone
    "3 days before" something that is already due). This is the existing,
    unchanged behavior; the fix does not introduce a special case for it."""
    slightly_past_due = datetime.now(timezone.utc) - timedelta(hours=2)
    client = FakeClient([COURSE], coursework={"course-1": [_assignment("cw-recent", "Just Missed It", slightly_past_due)]})

    sync_classroom(conn, client)

    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-recent'").fetchone()
    reminders = conn.execute("SELECT * FROM reminders WHERE task_id = ?", (task["id"],)).fetchall()
    assert reminders == []


def test_future_assignment_gets_normal_reminder_cadence(conn):
    due_in_10_days = datetime.now(timezone.utc) + timedelta(days=10)
    client = FakeClient([COURSE], coursework={"course-1": [_assignment("cw-future", "Upcoming Work", due_in_10_days)]})

    sync_classroom(conn, client)

    task = conn.execute("SELECT * FROM tasks WHERE external_id = 'cw-future'").fetchone()
    reminders = conn.execute(
        "SELECT reminder_type, status FROM reminders WHERE task_id = ?", (task["id"],)
    ).fetchall()
    types = {r["reminder_type"] for r in reminders}
    assert "T_MINUS_3D" in types
    assert "FINAL_1H" in types
    assert all(r["status"] == "PENDING" for r in reminders)


def test_repeated_sync_produces_no_duplicate_reminders(conn):
    due_in_10_days = datetime.now(timezone.utc) + timedelta(days=10)
    client = FakeClient([COURSE], coursework={"course-1": [_assignment("cw-future", "Upcoming Work", due_in_10_days)]})

    sync_classroom(conn, client)
    count1 = conn.execute("SELECT COUNT(*) AS c FROM reminders").fetchone()["c"]
    sync_classroom(conn, client)
    sync_classroom(conn, client)
    count2 = conn.execute("SELECT COUNT(*) AS c FROM reminders").fetchone()["c"]

    assert count1 > 0
    assert count1 == count2


def test_completed_and_cancelled_tasks_never_produce_notifications(conn):
    due_in_5_days = datetime.now(timezone.utc) + timedelta(days=5)
    client = FakeClient(
        [COURSE],
        coursework={
            "course-1": [
                _assignment("cw-done", "Will Complete", due_in_5_days),
                _assignment("cw-gone", "Will Cancel", due_in_5_days),
            ]
        },
    )
    sync_classroom(conn, client)
    done_task = conn.execute("SELECT id FROM tasks WHERE external_id = 'cw-done'").fetchone()
    gone_task = conn.execute("SELECT id FROM tasks WHERE external_id = 'cw-gone'").fetchone()

    repo.mark_completed(conn, task_id=done_task["id"])
    repo.cancel_pending_reminders(conn, task_id=done_task["id"])
    repo.cancel_task(conn, task_id=gone_task["id"])
    repo.cancel_pending_reminders(conn, task_id=gone_task["id"])

    from ragra.reminders.dispatch import preview_due_reminders

    far_future_now = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    previews = preview_due_reminders(conn, now=far_future_now)
    assert previews == []


def test_restart_preserves_backlog_suppressed_state(tmp_path):
    from ragra.db.connection import connect

    db_path = tmp_path / "restart-backlog-test.db"
    conn1 = connect(db_path)
    long_past_due = datetime.now(timezone.utc) - timedelta(days=200)
    client = FakeClient([COURSE], coursework={"course-1": [_assignment("cw-hist", "Old Homework", long_past_due)]})
    sync_classroom(conn1, client)
    conn1.close()

    conn2 = connect(db_path)
    task = conn2.execute("SELECT id FROM tasks WHERE external_id = 'cw-hist'").fetchone()
    reminders = conn2.execute("SELECT * FROM reminders WHERE task_id = ?", (task["id"],)).fetchall()
    assert reminders == []
    # Re-sync after "restart" - still no reminders resurrected.
    sync_classroom(conn2, client)
    reminders_after = conn2.execute("SELECT * FROM reminders WHERE task_id = ?", (task["id"],)).fetchall()
    assert reminders_after == []
    conn2.close()


def test_reconciliation_self_heals_reminders_scheduled_under_the_old_buggy_rule(conn):
    """Simulates pre-existing bad data: a task already overdue at discovery
    that nonetheless has PENDING reminders (as the old code, anchoring to
    Classroom's creationTime, could produce). A fresh sync must clean these
    up without touching a legitimately-current task's reminders."""
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE",
    )
    long_past_due = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    bad = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-bad",
        title="Bad Historical Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=long_past_due,
        source_published_at="2020-01-01T00:00:00+00:00", source_updated_at="2020-01-01T00:00:00+00:00",
    )
    repo.insert_reminder_if_absent(
        conn, task_id=bad.task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2020-01-05T00:00:00+00:00", idempotency_key="bad-key-1",
    )

    due_in_5_days = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    good = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-good",
        title="Legit Current Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=due_in_5_days,
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(),
    )
    repo.insert_reminder_if_absent(
        conn, task_id=good.task_id, reminder_type="T_MINUS_1D",
        scheduled_for=due_in_5_days, idempotency_key="good-key-1",
    )

    cancelled_count = repo.cancel_backlog_reminders_for_already_overdue_tasks(conn)
    assert cancelled_count == 1

    bad_reminder = conn.execute("SELECT status FROM reminders WHERE idempotency_key = 'bad-key-1'").fetchone()
    good_reminder = conn.execute("SELECT status FROM reminders WHERE idempotency_key = 'good-key-1'").fetchone()
    assert bad_reminder["status"] == "CANCELLED"
    assert good_reminder["status"] == "PENDING"

    # Idempotent - running it again cancels nothing further.
    assert repo.cancel_backlog_reminders_for_already_overdue_tasks(conn) == 0
