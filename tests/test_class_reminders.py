"""Tests for class-aware reminders.

Covers the rules that matter: one announcement per class occurrence ever,
never for a cancelled class, never after the class has already started, and
delivery strictly through the provider-neutral layer.
"""

from datetime import datetime, timedelta, timezone

from ragra.adapters.notify import NotifyResult
from ragra.db import repo
from ragra.reminders.class_reminders import (
    CLASS_SOON,
    class_reminder_message,
    dispatch_class_reminders,
    run_class_reminders,
    schedule_class_reminders,
)

# 2026-09-07 is a Monday. 08:30 PKT == 03:30 UTC.
CLASS_START_UTC = datetime(2026, 9, 7, 3, 30, tzinfo=timezone.utc)


class RecordingProvider:
    def __init__(self, result=None):
        self.result = result or NotifyResult(ok=True)
        self.calls = []

    def send(self, notification):
        self.calls.append(notification)
        return self.result


def _add_class(conn, *, external_id="tt-1", day_of_week=0, start="08:30", end="09:50", status="SCHEDULED"):
    repo.upsert_timetable_event(
        conn,
        external_id=external_id,
        course_name="DLD",
        program="CS",
        batch_year="2025",
        enrollment_type="REGULAR",
        day_of_week=day_of_week,
        occurrence_index=0,
        start_time=start,
        end_time=end,
        room="C-311",
        instructor=None,
        section="CS-G",
        status=status,
        source_spreadsheet_id="sheet-1",
        source_sheet_gid="1",
        source_sheet_title="Monday",
    )


def _pending(conn):
    return conn.execute("SELECT * FROM class_reminders").fetchall()


def test_class_within_the_window_is_claimed_once(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)

    assert schedule_class_reminders(conn, now=now) == 1
    # Re-running the tick claims nothing new - the occurrence is already
    # accounted for, so it can never be announced twice.
    assert schedule_class_reminders(conn, now=now) == 0
    assert len(_pending(conn)) == 1


def test_class_outside_the_window_is_not_claimed_yet(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(hours=6)

    assert schedule_class_reminders(conn, now=now) == 0
    assert _pending(conn) == []


def test_cancelled_class_never_produces_a_reminder(conn):
    _add_class(conn, status="CANCELLED")
    now = CLASS_START_UTC - timedelta(minutes=30)

    assert schedule_class_reminders(conn, now=now) == 0
    assert _pending(conn) == []


def test_claimed_reminder_is_delivered_through_the_provider_layer(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)
    schedule_class_reminders(conn, now=now)

    provider = RecordingProvider()
    summary = dispatch_class_reminders(conn, providers=[provider], now=now)

    assert summary.sent == 1
    assert len(provider.calls) == 1
    assert provider.calls[0].category == CLASS_SOON
    assert "DLD" in provider.calls[0].text
    assert "C-311" in provider.calls[0].text
    assert _pending(conn)[0]["status"] == "SENT"


def test_a_sent_reminder_is_never_sent_again(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)
    provider = RecordingProvider()

    run_class_reminders(conn, providers=[provider], now=now)
    run_class_reminders(conn, providers=[provider], now=now + timedelta(minutes=5))
    run_class_reminders(conn, providers=[provider], now=now + timedelta(minutes=10))

    assert len(provider.calls) == 1


def test_reminder_for_a_class_that_already_started_is_expired_not_sent(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)
    schedule_class_reminders(conn, now=now)

    provider = RecordingProvider()
    summary = dispatch_class_reminders(
        conn, providers=[provider], now=CLASS_START_UTC + timedelta(minutes=1)
    )

    assert provider.calls == []  # a late "starts soon" alert is worse than none
    assert summary.expired == 1
    assert _pending(conn)[0]["status"] == "FAILED"


def test_no_provider_configured_leaves_the_reminder_pending(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)
    schedule_class_reminders(conn, now=now)

    summary = dispatch_class_reminders(conn, providers=[], now=now)

    assert summary.skipped_not_configured == 1
    assert _pending(conn)[0]["status"] == "PENDING"  # goes out once configured


def test_failed_send_is_retried_while_the_class_is_still_ahead(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=40)
    schedule_class_reminders(conn, now=now)

    failing = RecordingProvider(NotifyResult(ok=False, error="channel down"))
    first = dispatch_class_reminders(conn, providers=[failing], now=now)
    assert first.retrying == 1
    assert _pending(conn)[0]["status"] == "PENDING"

    succeeding = RecordingProvider()
    second = dispatch_class_reminders(
        conn, providers=[succeeding], now=now + timedelta(minutes=15)
    )
    assert second.sent == 1


def test_delivers_via_any_successful_provider_when_others_fail(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)
    schedule_class_reminders(conn, now=now)

    failing = RecordingProvider(NotifyResult(ok=False, error="channel A down"))
    succeeding = RecordingProvider()
    summary = dispatch_class_reminders(conn, providers=[failing, succeeding], now=now)

    assert summary.sent == 1
    assert len(failing.calls) == 1  # attempted, not skipped


def test_stale_pending_reminder_is_expired_by_a_later_run(conn):
    _add_class(conn)
    now = CLASS_START_UTC - timedelta(minutes=30)
    schedule_class_reminders(conn, now=now)

    # Nothing was configured at the time, so it sat PENDING past the class.
    summary = run_class_reminders(
        conn, providers=[], now=CLASS_START_UTC + timedelta(hours=2)
    )

    assert summary.expired >= 1
    assert _pending(conn)[0]["status"] == "FAILED"


def test_message_states_the_real_start_time_and_room():
    now = CLASS_START_UTC - timedelta(minutes=32)
    text = class_reminder_message(
        course_name="DLD", starts_at_utc=CLASS_START_UTC, room="C-311", now=now
    )

    assert "DLD" in text
    assert "32 min" in text
    assert "8:30 AM" in text  # campus time, not the 03:30 UTC instant
    assert "PKT" in text
    assert "C-311" in text


def test_message_survives_a_class_with_no_room():
    text = class_reminder_message(
        course_name="DLD", starts_at_utc=CLASS_START_UTC, room=None,
        now=CLASS_START_UTC - timedelta(minutes=30),
    )
    assert "DLD" in text
    assert text.endswith(")")
