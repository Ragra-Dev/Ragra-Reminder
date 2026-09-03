"""Tests for ragra.reminders.dispatch - the piece that turns a PENDING,
due reminder row into an actual (or attempted) notification, with a
bounded retry policy for genuine delivery failures.

Uses FakeProvider (a NotificationProvider test double) rather than
monkeypatching Hermes specifics - dispatch.py never imports Hermes, so
these tests exercise the real provider-neutral contract.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ragra.adapters.notify import Notification, NotifyResult
from ragra.db import repo
from ragra.reminders.dispatch import MAX_ATTEMPTS, RETRY_DELAY, dispatch_due_reminders

from tests.support import owner_id


@dataclass
class FakeProvider:
    """Test double satisfying NotificationProvider. `result` may be a fixed
    NotifyResult or a zero-arg callable for behavior that varies across
    calls (e.g. fails once, then succeeds). Records the text of every
    notification passed to send() so a test can assert exactly how many real
    attempts happened."""

    result: NotifyResult | Callable[[], NotifyResult]
    calls: list[str] = field(default_factory=list)

    def send(self, notification: Notification) -> NotifyResult:
        self.calls.append(notification.text)
        return self.result() if callable(self.result) else self.result


def _make_task_with_reminder(conn, *, scheduled_for, status="ACTION_REQUIRED"):
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment 2", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    task_id = result.task_id
    if status != "ACTION_REQUIRED":
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
    repo.insert_reminder_if_absent(
        conn, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for=scheduled_for, idempotency_key=f"{task_id}:T_MINUS_1D:v1", user_id=owner_id(conn),
    )
    return task_id


NOW = "2026-09-09T12:00:00+00:00"
PAST = "2026-09-09T08:00:00+00:00"
FUTURE = "2026-12-01T00:00:00+00:00"


def test_not_configured_leaves_reminder_pending_and_is_reported(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    summary = dispatch_due_reminders(conn, providers=[], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 0
    assert summary.skipped_not_configured == 1
    row = conn.execute("SELECT status FROM reminders").fetchone()
    assert row["status"] == "PENDING"


def test_successful_send_marks_reminder_sent(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    provider = FakeProvider(NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 1
    row = conn.execute("SELECT status, sent_at FROM reminders").fetchone()
    assert row["status"] == "SENT"
    assert row["sent_at"] is not None


def test_dispatch_is_idempotent_never_resends_a_sent_reminder(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    provider = FakeProvider(NotifyResult(ok=True))

    first = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))
    second = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))
    third = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert first.sent == 1
    assert second.sent == 0
    assert third.sent == 0
    assert len(provider.calls) == 1  # the actual send only ever happened once

    count = conn.execute("SELECT COUNT(*) AS c FROM reminders WHERE status = 'SENT'").fetchone()["c"]
    assert count == 1


def test_failed_send_retries_rather_than_failing_immediately(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    provider = FakeProvider(NotifyResult(ok=False, error="platform rejected the message"))

    first = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert first.sent == 0
    assert first.retrying == 1
    assert first.permanently_failed == 0
    row = conn.execute("SELECT status, attempt_count, next_retry_at, last_error FROM reminders").fetchone()
    assert row["status"] == "PENDING"  # not permanently failed - eligible again later
    assert row["attempt_count"] == 1
    assert row["next_retry_at"] is not None
    assert row["last_error"] == "platform rejected the message"


def test_retrying_reminder_is_not_picked_up_again_before_its_backoff_elapses(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    provider = FakeProvider(NotifyResult(ok=False, error="transient"))

    dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))
    # Re-running immediately (before the retry backoff elapses) must not
    # attempt again - that would be a duplicate send attempt.
    again = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert again.retrying == 0
    assert again.sent == 0
    assert len(provider.calls) == 1


def test_retry_eventually_succeeds_once_backoff_elapses(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    attempts = {"n": 0}

    def fake_result():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return NotifyResult(ok=False, error="transient network error")
        return NotifyResult(ok=True)

    provider = FakeProvider(fake_result)

    dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))
    later = (datetime.fromisoformat(NOW) + RETRY_DELAY + timedelta(minutes=1)).isoformat()
    result = dispatch_due_reminders(conn, providers=[provider], now=later, user_id=owner_id(conn))

    assert result.sent == 1
    assert attempts["n"] == 2  # exactly one retry attempt, not more
    row = conn.execute("SELECT status, attempt_count FROM reminders").fetchone()
    assert row["status"] == "SENT"
    assert row["attempt_count"] == 1  # only the failed attempt counted; the success didn't increment it


def test_retry_exhausted_becomes_permanently_failed_and_stops(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    provider = FakeProvider(NotifyResult(ok=False, error="platform rejected the message"))

    now_dt = datetime.fromisoformat(NOW)
    last_summary = None
    for _attempt in range(MAX_ATTEMPTS):
        last_summary = dispatch_due_reminders(conn, providers=[provider], now=now_dt.isoformat(), user_id=owner_id(conn))
        now_dt = now_dt + RETRY_DELAY + timedelta(minutes=1)

    # Exactly MAX_ATTEMPTS real send attempts happened - no duplicates, no
    # extra attempts beyond the bound.
    assert len(provider.calls) == MAX_ATTEMPTS
    assert last_summary.permanently_failed == 1
    row = conn.execute("SELECT status, attempt_count, last_error FROM reminders").fetchone()
    assert row["status"] == "FAILED"
    assert row["attempt_count"] == MAX_ATTEMPTS
    assert row["last_error"] == "platform rejected the message"

    # A FAILED reminder is never picked up again, even long after the
    # backoff window - it is a genuinely terminal, surfaced failure.
    far_future = (now_dt + timedelta(days=1)).isoformat()
    final = dispatch_due_reminders(conn, providers=[provider], now=far_future, user_id=owner_id(conn))
    assert final.sent == 0
    assert final.retrying == 0
    assert final.permanently_failed == 0
    assert len(provider.calls) == MAX_ATTEMPTS  # no further attempts


def test_future_reminder_is_not_dispatched_yet(conn):
    _make_task_with_reminder(conn, scheduled_for=FUTURE)
    provider = FakeProvider(NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 0
    row = conn.execute("SELECT status FROM reminders").fetchone()
    assert row["status"] == "PENDING"


def test_completed_task_reminder_is_never_dispatched(conn):
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST)
    repo.mark_completed(conn, task_id=task_id, user_id=owner_id(conn))
    # Even if a PENDING row somehow still exists (defense in depth - the
    # normal path also calls cancel_pending_reminders on completion).
    provider = FakeProvider(NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 0
    assert provider.calls == []


def test_cancelled_task_reminder_is_never_dispatched(conn):
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST)
    repo.cancel_task_from_source(conn, task_id=task_id, user_id=owner_id(conn))
    provider = FakeProvider(NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 0
    assert provider.calls == []


def test_missed_task_reminder_is_never_dispatched(conn):
    # A task already past its deadline (MISSED) must never fire a "due
    # soon"/"due in 1 hour" reminder for a deadline that has already
    # passed - that would misrepresent historical/past work as current
    # actionable work.
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST, status="MISSED")
    provider = FakeProvider(NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 0
    assert provider.calls == []


def test_archived_course_reminder_is_never_dispatched(conn):
    # A task whose course has gone ARCHIVED at the source must not generate
    # a normal reminder - only currently active/enrolled courses are
    # eligible.
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ARCHIVED", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment 2", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    repo.insert_reminder_if_absent(
        conn, task_id=result.task_id, reminder_type="T_MINUS_1D",
        scheduled_for=PAST, idempotency_key=f"{result.task_id}:T_MINUS_1D:v1", user_id=owner_id(conn),
    )
    provider = FakeProvider(NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, providers=[provider], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 0
    assert provider.calls == []


def test_delivers_via_any_successful_provider_when_others_fail(conn):
    # Deliberate redundancy: every configured provider is attempted, and
    # the reminder is marked SENT if at least one succeeds - so one channel
    # breaking (e.g. Hermes) doesn't silently take down delivery as long as
    # a second provider is configured.
    _make_task_with_reminder(conn, scheduled_for=PAST)
    failing = FakeProvider(NotifyResult(ok=False, error="channel A down"))
    succeeding = FakeProvider(NotifyResult(ok=True))

    summary = dispatch_due_reminders(conn, providers=[failing, succeeding], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 1
    assert len(failing.calls) == 1  # it was attempted, not skipped
    assert len(succeeding.calls) == 1
    row = conn.execute("SELECT status FROM reminders").fetchone()
    assert row["status"] == "SENT"


def test_retries_when_every_configured_provider_fails(conn):
    _make_task_with_reminder(conn, scheduled_for=PAST)
    provider_a = FakeProvider(NotifyResult(ok=False, error="channel A down"))
    provider_b = FakeProvider(NotifyResult(ok=False, error="channel B down"))

    summary = dispatch_due_reminders(conn, providers=[provider_a, provider_b], now=NOW, user_id=owner_id(conn))

    assert summary.sent == 0
    assert summary.retrying == 1
    row = conn.execute("SELECT status, last_error FROM reminders").fetchone()
    assert row["status"] == "PENDING"
    assert "channel A down" in row["last_error"]
    assert "channel B down" in row["last_error"]


def test_personal_deadline_change_does_not_touch_reminders(conn):
    task_id = _make_task_with_reminder(conn, scheduled_for=PAST)
    before = conn.execute("SELECT id, status, scheduled_for FROM reminders").fetchall()

    repo.set_personal_deadline(conn, task_id=task_id, personal_deadline="2026-09-05", user_id=owner_id(conn))

    after = conn.execute("SELECT id, status, scheduled_for FROM reminders").fetchall()
    assert [dict(r) for r in before] == [dict(r) for r in after]


def test_restart_recovery_reminder_state_persists_across_reconnect(tmp_path):
    from ragra.db.connection import connect

    db_path = tmp_path / "restart-test.db"
    conn1 = connect(db_path)
    task_id = _make_task_with_reminder(conn1, scheduled_for=PAST)
    dispatch_due_reminders(conn1, providers=[FakeProvider(NotifyResult(ok=True))], now=NOW, user_id=owner_id(conn1))
    conn1.close()

    # Simulate a process restart: fresh connection to the same database file.
    conn2 = connect(db_path)
    row = conn2.execute("SELECT status FROM reminders WHERE task_id = ?", (task_id,)).fetchone()
    assert row["status"] == "SENT"

    # And dispatch again after "restart" - still no resend.
    provider = FakeProvider(NotifyResult(ok=True))
    summary = dispatch_due_reminders(conn2, providers=[provider], now=NOW, user_id=owner_id(conn2))
    assert summary.sent == 0
    assert provider.calls == []
    conn2.close()
