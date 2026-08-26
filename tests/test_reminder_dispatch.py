"""Tests for ragra.reminders.dispatch - the piece that turns a PENDING,
due reminder row into an actual (or attempted) notification, with a
bounded retry policy for genuine delivery failures.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ragra.adapters.notify import NotifyResult
from ragra.db import repo
from ragra.reminders import dispatch as dispatch_module
from ragra.reminders.dispatch import MAX_ATTEMPTS, RETRY_DELAY, dispatch_due_reminders


def _make_task_with_reminder(conn, *, scheduled_for, status="ACTION_REQUIRED"):
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE",
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment 2", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00",
    )
    task_id = result.task_id
    if status != "ACTION_REQUIRED":
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
    repo.insert_reminder_if_absent(
        conn, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for=scheduled_for, idempotency_key=f"{task_id}:T_MINUS_1D:v1",
    )
    return task_id


NOW = "2026-09-09T12:00:00+00:00"
PAST = "2026-09-09T08:00:00+00:00"
FUTURE = "2026-12-01T00:00:00+00:00"


def test_not_configured_leaves_reminder_pending_and_is_reported(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    summary = dispatch_due_reminders(conn, hermes_bin=None, notify_target=None, now=NOW)

    assert summary.sent == 0
    assert summary.skipped_not_configured == 1
    row = conn.execute("SELECT status FROM reminders").fetchone()
    assert row["status"] == "PENDING"


def test_successful_send_marks_reminder_sent(conn, monkeypatch):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    monkeypatch.setattr(dispatch_module, "send_notification", lambda **kw: NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert summary.sent == 1
    row = conn.execute("SELECT status, sent_at FROM reminders").fetchone()
    assert row["status"] == "SENT"
    assert row["sent_at"] is not None


def test_dispatch_is_idempotent_never_resends_a_sent_reminder(conn, monkeypatch):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    calls = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (calls.append(1), NotifyResult(ok=True))[1],
    )

    first = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)
    second = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)
    third = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert first.sent == 1
    assert second.sent == 0
    assert third.sent == 0
    assert len(calls) == 1  # the actual send only ever happened once

    count = conn.execute("SELECT COUNT(*) AS c FROM reminders WHERE status = 'SENT'").fetchone()["c"]
    assert count == 1


def test_failed_send_retries_rather_than_failing_immediately(conn, monkeypatch):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: NotifyResult(ok=False, error="platform rejected the message"),
    )

    first = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert first.sent == 0
    assert first.retrying == 1
    assert first.permanently_failed == 0
    row = conn.execute("SELECT status, attempt_count, next_retry_at, last_error FROM reminders").fetchone()
    assert row["status"] == "PENDING"  # not permanently failed - eligible again later
    assert row["attempt_count"] == 1
    assert row["next_retry_at"] is not None
    assert row["last_error"] == "platform rejected the message"


def test_retrying_reminder_is_not_picked_up_again_before_its_backoff_elapses(conn, monkeypatch):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    calls = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (calls.append(1), NotifyResult(ok=False, error="transient"))[1],
    )

    dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)
    # Re-running immediately (before the retry backoff elapses) must not
    # attempt again - that would be a duplicate send attempt.
    again = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert again.retrying == 0
    assert again.sent == 0
    assert len(calls) == 1


def test_retry_eventually_succeeds_once_backoff_elapses(conn, monkeypatch):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    attempts = {"n": 0}

    def fake_send(**kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return NotifyResult(ok=False, error="transient network error")
        return NotifyResult(ok=True)

    monkeypatch.setattr(dispatch_module, "send_notification", fake_send)

    dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)
    later = (datetime.fromisoformat(NOW) + RETRY_DELAY + timedelta(minutes=1)).isoformat()
    result = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=later)

    assert result.sent == 1
    assert attempts["n"] == 2  # exactly one retry attempt, not more
    row = conn.execute("SELECT status, attempt_count FROM reminders").fetchone()
    assert row["status"] == "SENT"
    assert row["attempt_count"] == 1  # only the failed attempt counted; the success didn't increment it


def test_retry_exhausted_becomes_permanently_failed_and_stops(conn, monkeypatch):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    calls = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (calls.append(1), NotifyResult(ok=False, error="platform rejected the message"))[1],
    )

    now_dt = datetime.fromisoformat(NOW)
    last_summary = None
    for attempt in range(MAX_ATTEMPTS):
        last_summary = dispatch_due_reminders(
            conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=now_dt.isoformat()
        )
        now_dt = now_dt + RETRY_DELAY + timedelta(minutes=1)

    # Exactly MAX_ATTEMPTS real send attempts happened - no duplicates, no
    # extra attempts beyond the bound.
    assert len(calls) == MAX_ATTEMPTS
    assert last_summary.permanently_failed == 1
    row = conn.execute("SELECT status, attempt_count, last_error FROM reminders").fetchone()
    assert row["status"] == "FAILED"
    assert row["attempt_count"] == MAX_ATTEMPTS
    assert row["last_error"] == "platform rejected the message"

    # A FAILED reminder is never picked up again, even long after the
    # backoff window - it is a genuinely terminal, surfaced failure.
    far_future = (now_dt + timedelta(days=1)).isoformat()
    final = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=far_future)
    assert final.sent == 0
    assert final.retrying == 0
    assert final.permanently_failed == 0
    assert len(calls) == MAX_ATTEMPTS  # no further attempts


def test_future_reminder_is_not_dispatched_yet(conn, monkeypatch):
    _make_task_with_reminder(conn, scheduled_for=FUTURE)
    monkeypatch.setattr(dispatch_module, "send_notification", lambda **kw: NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert summary.sent == 0
    row = conn.execute("SELECT status FROM reminders").fetchone()
    assert row["status"] == "PENDING"


def test_completed_task_reminder_is_never_dispatched(conn, monkeypatch):
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST)
    repo.mark_completed(conn, task_id=task_id)
    # Even if a PENDING row somehow still exists (defense in depth - the
    # normal path also calls cancel_pending_reminders on completion).
    sends = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (sends.append(1), NotifyResult(ok=True))[1],
    )

    summary = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert summary.sent == 0
    assert sends == []


def test_cancelled_task_reminder_is_never_dispatched(conn, monkeypatch):
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST)
    repo.cancel_task(conn, task_id=task_id)
    sends = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (sends.append(1), NotifyResult(ok=True))[1],
    )

    summary = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert summary.sent == 0
    assert sends == []


def test_missed_task_reminder_is_never_dispatched(conn, monkeypatch):
    # A task already past its deadline (MISSED) must never fire a "due
    # soon"/"due in 1 hour" reminder for a deadline that has already
    # passed - that would misrepresent historical/past work as current
    # actionable work.
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST, status="MISSED")
    sends = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (sends.append(1), NotifyResult(ok=True))[1],
    )

    summary = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert summary.sent == 0
    assert sends == []


def test_archived_course_reminder_is_never_dispatched(conn, monkeypatch):
    # A task whose course has gone ARCHIVED at the source must not generate
    # a normal reminder - only currently active/enrolled courses are
    # eligible.
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ARCHIVED",
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment 2", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00",
    )
    repo.insert_reminder_if_absent(
        conn, task_id=result.task_id, reminder_type="T_MINUS_1D",
        scheduled_for=PAST, idempotency_key=f"{result.task_id}:T_MINUS_1D:v1",
    )
    sends = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (sends.append(1), NotifyResult(ok=True))[1],
    )

    summary = dispatch_due_reminders(conn, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)

    assert summary.sent == 0
    assert sends == []


def test_personal_deadline_change_does_not_touch_reminders(conn):
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST)
    before = conn.execute("SELECT id, status, scheduled_for FROM reminders").fetchall()

    repo.set_personal_deadline(conn, task_id=task_id, personal_deadline="2026-09-05")

    after = conn.execute("SELECT id, status, scheduled_for FROM reminders").fetchall()
    assert [dict(r) for r in before] == [dict(r) for r in after]


def test_restart_recovery_reminder_state_persists_across_reconnect(tmp_path, monkeypatch):
    from ragra.db.connection import connect

    db_path = tmp_path / "restart-test.db"
    conn1 = connect(db_path)
    task_id = _make_task_with_reminder(conn1, scheduled_for=PAST)
    monkeypatch.setattr(dispatch_module, "send_notification", lambda **kw: NotifyResult(ok=True))
    dispatch_due_reminders(conn1, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)
    conn1.close()

    # Simulate a process restart: fresh connection to the same database file.
    conn2 = connect(db_path)
    row = conn2.execute("SELECT status FROM reminders WHERE task_id = ?", (task_id,)).fetchone()
    assert row["status"] == "SENT"

    # And dispatch again after "restart" - still no resend.
    sends = []
    monkeypatch.setattr(
        dispatch_module, "send_notification",
        lambda **kw: (sends.append(1), NotifyResult(ok=True))[1],
    )
    summary = dispatch_due_reminders(conn2, hermes_bin=Path("hermes.exe"), notify_target="telegram", now=NOW)
    assert summary.sent == 0
    assert sends == []
    conn2.close()
