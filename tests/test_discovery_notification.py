"""The discovery-notification invariant, end to end through sync.

Every newly discovered ACTIONABLE task gets exactly one NEW_ASSIGNMENT
reminder - once, ever, no matter how many times it is re-synced or how
often Classroom moves its deadline afterwards.

These are integration tests on purpose. The unit-level cadence is covered
in tests/test_reminder_engine.py; what is covered here is the part that
actually failed in production - the interaction between the engine's plan,
the idempotency key the sync layer builds from it, and the cancellation
that a deadline change triggers. A defect in any one of those three
produces either a missing announcement or a duplicate one, and neither is
visible from the engine alone.
"""

from datetime import datetime, timedelta, timezone

from ragra.db import repo
from ragra.sync.classroom_sync import sync_classroom
from tests.support import owner_id
from tests.test_relevance_persistence import FakeClassroomClient, _item


class AnnouncementOnlyClient(FakeClassroomClient):
    """Same double, but serving an announcement instead of coursework -
    the INFORMATIONAL path, which carries no deadline."""

    def __init__(self, announcements):
        super().__init__()
        self._announcements = announcements

    def list_announcements(self, course_id):
        return self._announcements


def _coursework(external_id, *, days_out=0, hours_out=0, updated="2026-09-01T00:00:00Z", title="Assignment 1"):
    """Coursework due `days_out` days / `hours_out` hours from now.

    Deliberately relative to the real clock: sync anchors the reminder
    cadence to its own "now", so a hardcoded calendar date silently turns
    into historical backlog once that date passes - which is exactly how an
    earlier draft of these tests started asserting the wrong branch. A zero
    offset is genuinely backlog by the time sync runs, so a "short lead"
    case has to be hours out, not zero.
    """
    due = datetime.now(timezone.utc) + timedelta(days=days_out, hours=hours_out)
    item = _item(external_id, title, updated=updated)
    item["dueDate"] = {"year": due.year, "month": due.month, "day": due.day}
    item["dueTime"] = {"hours": due.hour, "minutes": due.minute}
    return item


def _announcements(conn, task_title=None):
    rows = conn.execute(
        "SELECT * FROM reminders WHERE reminder_type = 'NEW_ASSIGNMENT' ORDER BY id"
    ).fetchall()
    return rows


def _types(conn):
    return [
        r["reminder_type"]
        for r in conn.execute("SELECT reminder_type FROM reminders ORDER BY id")
    ]


# ---------------------------------------------------------------------------
# A/B/C - discovery announcement across every lead time, through real sync
# ---------------------------------------------------------------------------


def test_long_lead_assignment_is_announced(conn):
    """The exact shape of the production defect: an assignment posted well
    ahead of its deadline. Previously produced no announcement at all."""
    client = FakeClassroomClient(
        coursework=[_coursework("cw-oop", days_out=16)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))

    assert len(_announcements(conn)) == 1
    # and the countdown still exists alongside it
    assert "T_MINUS_7D" in _types(conn)


def test_medium_lead_assignment_is_announced(conn):
    client = FakeClassroomClient(
        coursework=[_coursework("cw-mid", days_out=2)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))
    assert len(_announcements(conn)) == 1


def test_short_lead_assignment_is_announced(conn):
    client = FakeClassroomClient(
        coursework=[_coursework("cw-short", hours_out=6)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))
    assert len(_announcements(conn)) == 1


# ---------------------------------------------------------------------------
# D/F/G - exactly once, across resyncs, restarts and repeated ticks
# ---------------------------------------------------------------------------


def test_repeated_sync_never_announces_the_same_task_twice(conn):
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=16)]
    )
    for _ in range(5):
        sync_classroom(conn, client, user_id=owner_id(conn))

    assert len(_announcements(conn)) == 1


def test_a_resync_after_restart_still_announces_only_once(conn):
    """Idempotency must come from durable state, not from anything held in
    memory for the lifetime of one sync run."""
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=16)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))

    # A brand-new client object stands in for a restarted process; the
    # database is the only thing carried over.
    fresh_client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=16)]
    )
    sync_classroom(conn, fresh_client, user_id=owner_id(conn))

    assert len(_announcements(conn)) == 1


# ---------------------------------------------------------------------------
# E/F - a deadline change must not re-announce an already-known task
# ---------------------------------------------------------------------------


def test_a_deadline_change_does_not_re_announce_the_task(conn):
    """The second half of the production defect: a rescheduled task
    announced itself as new all over again, because its announcement key
    included the deadline that had just moved."""
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=10)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))
    assert len(_announcements(conn)) == 1

    client._coursework = [_coursework("cw-1", days_out=15, updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client, user_id=owner_id(conn))

    assert len(_announcements(conn)) == 1, "a moved deadline is not a new assignment"
    assert _types(conn).count("DEADLINE_CHANGED") == 1, "the change itself is still announced"


def test_repeated_ticks_after_a_deadline_change_still_announce_once(conn):
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=10)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))
    client._coursework = [_coursework("cw-1", days_out=15, updated="2026-09-02T00:00:00Z")]
    for _ in range(4):
        sync_classroom(conn, client, user_id=owner_id(conn))

    assert len(_announcements(conn)) == 1


def test_several_deadline_changes_still_announce_the_task_once(conn):
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=10)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))
    for day, updated in ((15, "2026-09-02T00:00:00Z"), (18, "2026-09-03T00:00:00Z")):
        client._coursework = [_coursework("cw-1", days_out=day, updated=updated)]
        sync_classroom(conn, client, user_id=owner_id(conn))

    assert len(_announcements(conn)) == 1
    assert _types(conn).count("DEADLINE_CHANGED") == 2  # each real change still alerts


def test_the_announcement_survives_a_deadline_change_before_it_is_sent(conn):
    """The subtle one. A deadline change cancels the countdown, which is
    correct - but the announcement is not a countdown, and cancelling it
    while task-anchored idempotency then declines to re-create it would
    drop the notification entirely."""
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=10)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))
    client._coursework = [_coursework("cw-1", days_out=15, updated="2026-09-02T00:00:00Z")]
    sync_classroom(conn, client, user_id=owner_id(conn))

    announcements = _announcements(conn)
    assert len(announcements) == 1
    assert announcements[0]["status"] == "PENDING", "still deliverable, not cancelled"


# ---------------------------------------------------------------------------
# H - the generation-side invariant actually catches a missing announcement
# ---------------------------------------------------------------------------


def test_the_invariant_reports_a_missing_discovery_notification(conn, monkeypatch):
    """Deliberately break generation and prove the signal fires.

    Without this the check would be untested in the only state that
    matters - every downstream metric stays green when a notification is
    never created, which is exactly how the real defect survived a clean
    29-hour soak.
    """
    from ragra.reminders import engine as engine_module
    from ragra.sync import classroom_sync as sync_module

    real_plan = engine_module.compute_reminder_plan

    def plan_without_announcement(*, actual_deadline, discovered_at):
        return [
            p
            for p in real_plan(actual_deadline=actual_deadline, discovered_at=discovered_at)
            if p.reminder_type != "NEW_ASSIGNMENT"
        ]

    monkeypatch.setattr(sync_module, "compute_reminder_plan", plan_without_announcement)

    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=16)]
    )
    summary = sync_classroom(conn, client, user_id=owner_id(conn))

    assert len(summary.missing_discovery_notification) == 1
    assert "NEW_ASSIGNMENT" in summary.missing_discovery_notification[0]


def test_a_healthy_sync_reports_no_missing_discovery_notification(conn):
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=16)]
    )
    summary = sync_classroom(conn, client, user_id=owner_id(conn))
    assert summary.missing_discovery_notification == []


def test_the_invariant_is_not_satisfied_by_some_other_reminder_type(conn):
    """A countdown reminder existing must never be mistaken for the
    discovery announcement - the check is type-exact."""
    client = FakeClassroomClient(
        coursework=[_coursework("cw-1", days_out=16)]
    )
    sync_classroom(conn, client, user_id=owner_id(conn))
    task_id = conn.execute("SELECT id FROM tasks WHERE external_id = 'cw-1'").fetchone()["id"]

    assert repo.has_reminder_of_type(
        conn, user_id=owner_id(conn), task_id=task_id, reminder_type="T_MINUS_7D"
    )
    conn.execute(
        "DELETE FROM reminders WHERE task_id = ? AND reminder_type = 'NEW_ASSIGNMENT'", (task_id,)
    )
    conn.commit()

    assert not repo.has_reminder_of_type(
        conn, user_id=owner_id(conn), task_id=task_id, reminder_type="NEW_ASSIGNMENT"
    )


# ---------------------------------------------------------------------------
# I - informational tasks must not trip the invariant
# ---------------------------------------------------------------------------


def test_announcements_do_not_trigger_a_missing_notification_alarm(conn):
    """Announcements carry no deadline and are a dashboard workflow, not a
    notification channel. They must stay silent without being reported as
    a defect."""
    client = AnnouncementOnlyClient(
        [_item("ann-1", "Quiz on Tuesday", updated="2026-09-01T00:00:00Z")]
    )
    summary = sync_classroom(conn, client, user_id=owner_id(conn))

    assert summary.missing_discovery_notification == []
    assert _announcements(conn) == []


def test_an_already_overdue_task_is_exempt_not_reported(conn):
    """Historical backlog: past its deadline the moment Ragra first sees
    it. compute_reminder_plan returns nothing by design, and that documented
    exemption must not be reported as a missing notification."""
    client = FakeClassroomClient(
        coursework=[_coursework("cw-old", days_out=-30, updated="2026-01-01T00:00:00Z")]
    )
    summary = sync_classroom(conn, client, user_id=owner_id(conn))

    assert summary.missing_discovery_notification == []
    assert _announcements(conn) == []
