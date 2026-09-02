"""Tests for deadline-change notifications.

Detection already existed; the notification did not. It is deliberately
routed through the existing reminders table rather than sent inline from
sync, so it inherits idempotency, bounded retry, terminal-failure handling
and delivery recording instead of reimplementing any of them - and so a
send failure is retried rather than silently lost.
"""

from ragra.adapters.notify import NotifyResult
from ragra.reminders.dispatch import dispatch_due_reminders
from ragra.reminders.engine import reminder_message
from ragra.sync.classroom_sync import sync_classroom
from tests.test_relevance_persistence import FakeClassroomClient, _item

NOW = "2026-12-02T12:00:00+00:00"


class RecordingProvider:
    def __init__(self, result=None):
        self.result = result or NotifyResult(ok=True)
        self.calls = []

    def send(self, notification):
        self.calls.append(notification)
        return self.result


def _dated_item(external_id, day, *, updated):
    item = _item(external_id, "Assignment 1", updated=updated)
    item["dueDate"] = {"year": 2026, "month": 12, "day": day}
    item["dueTime"] = {"hours": 23, "minutes": 59}
    return item


def _change_reminders(conn):
    return conn.execute(
        "SELECT * FROM reminders WHERE reminder_type = 'DEADLINE_CHANGED' ORDER BY id"
    ).fetchall()


def test_deadline_change_queues_exactly_one_notification(conn):
    client = FakeClassroomClient(coursework=[_dated_item("cw-1", 10, updated="2026-09-01T00:00:00Z")])
    sync_classroom(conn, client)
    assert _change_reminders(conn) == []  # first discovery is not a "change"

    client._coursework = [_dated_item("cw-1", 15, updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client)

    assert len(_change_reminders(conn)) == 1


def test_unchanged_deadline_never_queues_a_notification(conn):
    client = FakeClassroomClient(coursework=[_dated_item("cw-1", 10, updated="2026-09-01T00:00:00Z")])
    sync_classroom(conn, client)
    sync_classroom(conn, client)
    sync_classroom(conn, client)

    assert _change_reminders(conn) == []


def test_redetecting_the_same_change_does_not_duplicate(conn):
    client = FakeClassroomClient(coursework=[_dated_item("cw-1", 10, updated="2026-09-01T00:00:00Z")])
    sync_classroom(conn, client)

    changed = _dated_item("cw-1", 15, updated="2026-09-02T00:00:00Z")
    client._coursework = [changed]
    sync_classroom(conn, client)
    # Same change seen again with a newer updateTime but the same deadline.
    client._coursework = [_dated_item("cw-1", 15, updated="2026-09-03T00:00:00Z")]
    sync_classroom(conn, client)

    assert len(_change_reminders(conn)) == 1


def test_a_second_genuine_change_does_notify_again(conn):
    client = FakeClassroomClient(coursework=[_dated_item("cw-1", 10, updated="2026-09-01T00:00:00Z")])
    sync_classroom(conn, client)
    client._coursework = [_dated_item("cw-1", 15, updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client)
    client._coursework = [_dated_item("cw-1", 20, updated="2026-09-03T00:00:00Z")]
    sync_classroom(conn, client)

    assert len(_change_reminders(conn)) == 2


def test_deadline_change_notification_is_delivered_through_the_provider_layer(conn):
    client = FakeClassroomClient(coursework=[_dated_item("cw-1", 10, updated="2026-09-01T00:00:00Z")])
    sync_classroom(conn, client)
    client._coursework = [_dated_item("cw-1", 15, updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client)

    provider = RecordingProvider()
    summary = dispatch_due_reminders(conn, providers=[provider], now=NOW)

    assert summary.sent >= 1
    assert any("Deadline changed" in call.text for call in provider.calls)
    assert any(call.category == "DEADLINE_CHANGED" for call in provider.calls)


def test_old_reminders_are_cancelled_when_the_deadline_moves(conn):
    client = FakeClassroomClient(coursework=[_dated_item("cw-1", 10, updated="2026-09-01T00:00:00Z")])
    sync_classroom(conn, client)
    client._coursework = [_dated_item("cw-1", 15, updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client)

    # No PENDING countdown reminder may still point at the old deadline.
    stale = conn.execute(
        """SELECT COUNT(*) AS c FROM reminders
           WHERE status = 'PENDING' AND reminder_type != 'DEADLINE_CHANGED'
           AND idempotency_key LIKE '%2026-12-10%'"""
    ).fetchone()["c"]
    assert stale == 0


def test_failed_delivery_is_retried_not_lost(conn):
    client = FakeClassroomClient(coursework=[_dated_item("cw-1", 10, updated="2026-09-01T00:00:00Z")])
    sync_classroom(conn, client)
    client._coursework = [_dated_item("cw-1", 15, updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client)

    failing = RecordingProvider(NotifyResult(ok=False, error="channel down"))
    summary = dispatch_due_reminders(conn, providers=[failing], now=NOW)

    assert summary.sent == 0
    assert summary.retrying == 1
    assert _change_reminders(conn)[0]["status"] == "PENDING"  # still eligible


def test_message_text_states_what_happened():
    assert "Deadline changed" in reminder_message("DEADLINE_CHANGED", "Assignment 1", "CS1004")
