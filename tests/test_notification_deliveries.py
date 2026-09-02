"""Tests for notification delivery status.

The security-relevant one is test_delivery_rows_never_contain_a_credential:
this table is rendered in the dashboard, so it is a disclosure surface, not
merely a log. A provider error that carried an SMTP password into a row
would put a credential on a web page.
"""

import smtplib
from datetime import datetime, timedelta, timezone

from ragra.adapters.notify import EmailProvider, Notification, NotifyResult, send_to_all_providers
from ragra.db import repo
from ragra.reminders.class_reminders import schedule_class_reminders, dispatch_class_reminders
from ragra.reminders.dispatch import dispatch_due_reminders
from ragra import health
from tests.test_class_reminders import RecordingProvider, _add_class, CLASS_START_UTC

NOW = "2026-09-09T12:00:00+00:00"
PAST = "2026-09-09T08:00:00+00:00"


def _make_task_with_reminder(conn):
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
    repo.insert_reminder_if_absent(
        conn, task_id=result.task_id, reminder_type="T_MINUS_1D",
        scheduled_for=PAST, idempotency_key=f"{result.task_id}:T_MINUS_1D:v1",
    )
    return result.task_id


def test_one_row_is_recorded_per_provider_per_reminder(conn):
    _make_task_with_reminder(conn)
    first = RecordingProvider()
    second = RecordingProvider()

    dispatch_due_reminders(conn, providers=[first, second], now=NOW)

    rows = repo.recent_notification_deliveries(conn)
    assert len(rows) == 2
    assert {r["provider"] for r in rows} == {"RecordingProvider"}
    assert all(r["ok"] == 1 for r in rows)
    assert all(r["category"] == "T_MINUS_1D" for r in rows)


def test_failures_are_recorded_with_their_error(conn):
    _make_task_with_reminder(conn)
    failing = RecordingProvider(NotifyResult(ok=False, error="channel down"))

    dispatch_due_reminders(conn, providers=[failing], now=NOW)

    row = repo.recent_notification_deliveries(conn)[0]
    assert row["ok"] == 0
    assert row["error"] == "channel down"


def test_partial_failure_records_both_outcomes(conn):
    _make_task_with_reminder(conn)
    failing = RecordingProvider(NotifyResult(ok=False, error="channel A down"))
    succeeding = RecordingProvider()

    dispatch_due_reminders(conn, providers=[failing, succeeding], now=NOW)

    rows = repo.recent_notification_deliveries(conn)
    assert sorted(r["ok"] for r in rows) == [0, 1]


def test_deliveries_are_linked_to_their_reminder(conn):
    task_id = _make_task_with_reminder(conn)
    dispatch_due_reminders(conn, providers=[RecordingProvider()], now=NOW)

    reminder_id = conn.execute("SELECT id FROM reminders WHERE task_id = ?", (task_id,)).fetchone()["id"]
    assert len(repo.deliveries_for_reminder(conn, reminder_id=reminder_id)) == 1


def test_health_alert_delivery_is_recorded_without_a_reminder_id(conn):
    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom")

    health.check_and_alert(conn, providers=[RecordingProvider()])

    row = repo.recent_notification_deliveries(conn)[0]
    assert row["reminder_id"] is None  # health alerts have no reminder row
    assert row["category"] == "HEALTH_ALERT"
    assert row["ok"] == 1


def test_class_reminder_delivery_is_recorded(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)
    schedule_class_reminders(conn, now=now)

    dispatch_class_reminders(conn, providers=[RecordingProvider()], now=now)

    row = repo.recent_notification_deliveries(conn)[0]
    assert row["category"] == "CLASS_SOON"
    assert row["reminder_id"] is None
    assert row["ok"] == 1


def test_nothing_is_recorded_when_no_provider_is_configured(conn):
    _make_task_with_reminder(conn)
    dispatch_due_reminders(conn, providers=[], now=NOW)

    assert repo.recent_notification_deliveries(conn) == []


def test_delivery_rows_never_contain_a_credential(conn, monkeypatch):
    # The dashboard renders this table, so a leaked credential here is a
    # disclosure, not just a noisy log line.
    secret = "super-secret-password"

    class _FailingSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(535, f"auth failed for {secret}".encode())

        def send_message(self, message):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("ragra.adapters.notify.smtplib.SMTP", _FailingSMTP)
    _make_task_with_reminder(conn)
    provider = EmailProvider(
        host="smtp.example.com", port=587, from_address="ragra@example.com",
        to_address="student@example.com", username="ragra@example.com", password=secret,
    )

    dispatch_due_reminders(conn, providers=[provider], now=NOW)

    row = repo.recent_notification_deliveries(conn)[0]
    assert row["ok"] == 0
    assert secret not in (row["error"] or "")
    assert "***" in row["error"]
    # And nothing anywhere else in the row either.
    assert all(secret not in str(value) for value in tuple(row))


def test_a_broken_recorder_never_undoes_a_successful_send():
    # Bookkeeping failure must not turn a delivered message into a failure.
    def _explode(provider_name, result):
        raise RuntimeError("recorder is broken")

    delivered, errors = send_to_all_providers(
        [RecordingProvider()], Notification(text="hello"), on_attempt=_explode
    )

    assert delivered is True
    assert errors == []
