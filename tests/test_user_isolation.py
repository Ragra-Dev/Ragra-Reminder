"""Two-user isolation at the repository layer.

Every test here sets up the *same* situation for two independent accounts
and then asserts each one sees, changes, and counts only its own rows. The
setups are deliberately adversarial in the way real collisions are
adversarial: the two users are enrolled in the same Classroom course, sit
in the same FAST timetable slot, and generate identical idempotency keys -
because those are exactly the cases a global UNIQUE constraint or an
unfiltered WHERE clause gets wrong, and a suite built from two users with
unrelated data would pass while still leaking.

The complementary check lives in tests/test_user_scoping_guard.py: this
file proves the queries that exist today are scoped, that one proves no
future query can quietly not be.
"""

from __future__ import annotations

import pytest

from ragra.db import repo
from tests.support import make_user, owner_id


@pytest.fixture
def two_users(conn):
    """Two independent accounts. The first is the seeded pre-identity owner,
    so these tests also cover the account a migrated database's data
    belongs to, not just freshly created ones."""
    return owner_id(conn), make_user(conn, google_sub="isolation-second", display_name="Bea")


def _course(conn, user_id, external_id="course-shared"):
    return repo.upsert_course(
        conn,
        user_id=user_id,
        external_id=external_id,
        name="Digital Logic Design",
        section="A",
        teacher=None,
        course_code="EE1005",
        state="ACTIVE",
    )


def _task(conn, user_id, course_id, external_id="item-shared", deadline="2026-09-10T12:00:00+00:00"):
    return repo.upsert_task_from_source(
        conn,
        user_id=user_id,
        course_id=course_id,
        source_type="coursework",
        external_id=external_id,
        title="Lab 3",
        description=None,
        link=None,
        kind="ACTIONABLE",
        actual_deadline=deadline,
        source_published_at=None,
        source_updated_at=None,
    ).task_id


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------


def test_two_users_can_hold_the_same_classroom_course(conn, two_users):
    """Enrolment overlap is the normal case, not an edge case: any two
    students in one section share every course id Classroom issues."""
    alice, bea = two_users
    alice_course = _course(conn, alice)
    bea_course = _course(conn, bea)

    assert alice_course != bea_course
    owners = conn.execute(
        "SELECT user_id FROM courses WHERE external_id = 'course-shared' ORDER BY user_id"
    ).fetchall()
    assert [row["user_id"] for row in owners] == sorted([alice, bea])


def test_a_course_state_change_touches_only_one_users_row(conn, two_users):
    alice, bea = two_users
    _course(conn, alice)
    _course(conn, bea)

    repo.update_course_state_if_known(conn, user_id=alice, external_id="course-shared", state="ARCHIVED")

    states = {
        row["user_id"]: row["state"]
        for row in conn.execute("SELECT user_id, state FROM courses WHERE external_id = 'course-shared'")
    }
    assert states[alice] == "ARCHIVED"
    assert states[bea] == "ACTIVE"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def test_task_lookups_never_return_another_users_task(conn, two_users):
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))

    assert repo.get_task_by_id(conn, user_id=alice, task_id=alice_task) is not None
    assert repo.get_task_by_id(conn, user_id=bea, task_id=alice_task) is None


def test_completing_another_users_task_changes_nothing(conn, two_users):
    """The IDOR case stated plainly: knowing an id must not be enough."""
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))

    repo.mark_completed(conn, user_id=bea, task_id=alice_task)

    row = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (alice_task,)).fetchone()
    assert row["status"] != "COMPLETED"
    assert row["completed_at"] is None


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda user_id, task_id: ("mark_completed", {}), id="complete"),
        pytest.param(lambda user_id, task_id: ("mark_missed", {}), id="miss"),
        pytest.param(lambda user_id, task_id: ("cancel_task_from_source", {}), id="cancel"),
        pytest.param(
            lambda user_id, task_id: (
                "set_personal_deadline", {"personal_deadline": "2026-09-01T00:00:00+00:00"}
            ),
            id="personal-deadline",
        ),
    ],
)
def test_a_rejected_cross_user_write_leaves_no_history_behind(conn, two_users, operation):
    """A blocked write must leave no trace in the audit trail either.

    task_history is rendered on the task detail page and is meant to be an
    append-only record of changes that actually happened, so a row written
    for an UPDATE the ownership filter rejected would be doubly wrong: a
    false entry in the victim's history, and a row referencing the victim's
    task attributed to the caller. Every state-changing operation is covered
    because they all shared the same "update, then record history
    unconditionally" shape.
    """
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))
    before = conn.execute("SELECT COUNT(*) AS c FROM task_history").fetchone()["c"]

    name, kwargs = operation(bea, alice_task)
    getattr(repo, name)(conn, user_id=bea, task_id=alice_task, **kwargs)

    after = conn.execute("SELECT COUNT(*) AS c FROM task_history").fetchone()["c"]
    assert after == before, f"{name} wrote history for a rejected cross-user write"
    stray = conn.execute(
        "SELECT COUNT(*) AS c FROM task_history WHERE user_id = ?", (bea,)
    ).fetchone()["c"]
    assert stray == 0


def test_setting_a_personal_deadline_on_another_users_task_does_nothing(conn, two_users):
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))

    repo.set_personal_deadline(
        conn, user_id=bea, task_id=alice_task, personal_deadline="2026-09-01T00:00:00+00:00"
    )

    row = conn.execute("SELECT personal_deadline FROM tasks WHERE id = ?", (alice_task,)).fetchone()
    assert row["personal_deadline"] is None


def test_editing_another_users_manual_task_is_refused(conn, two_users):
    alice, bea = two_users
    alice_task = repo.create_manual_task(conn, user_id=alice, title="Alice's own plan")

    # Refused as "not yours", indistinguishable from "does not exist" - the
    # guard must not confirm that the id is real.
    with pytest.raises(repo.TaskSourceViolation):
        repo.update_manual_task(conn, user_id=bea, task_id=alice_task, title="hijacked")

    row = conn.execute("SELECT title FROM tasks WHERE id = ?", (alice_task,)).fetchone()
    assert row["title"] == "Alice's own plan"


def test_task_listings_are_per_user(conn, two_users):
    alice, bea = two_users
    _task(conn, alice, _course(conn, alice))
    repo.create_manual_task(conn, user_id=bea, title="Bea's own task")

    assert len(repo.manual_tasks(conn, user_id=alice)) == 0
    assert len(repo.manual_tasks(conn, user_id=bea)) == 1

    alice_due = repo.tasks_due_between(
        conn, user_id=alice, start_iso="2026-01-01T00:00:00+00:00", end_iso="2027-01-01T00:00:00+00:00"
    )
    bea_due = repo.tasks_due_between(
        conn, user_id=bea, start_iso="2026-01-01T00:00:00+00:00", end_iso="2027-01-01T00:00:00+00:00"
    )
    assert len(alice_due) == 1
    assert len(bea_due) == 0


def test_missed_counts_do_not_include_another_users_work(conn, two_users):
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice), deadline="2020-01-01T00:00:00+00:00")

    marked = repo.mark_overdue_tasks_as_missed(conn, user_id=alice, now="2026-09-01T00:00:00+00:00")

    assert marked == [alice_task]
    assert repo.count_missed_tasks(conn, user_id=alice) == 1
    assert repo.count_missed_tasks(conn, user_id=bea) == 0
    assert repo.missed_tasks(conn, user_id=bea) == []


def test_one_users_overdue_sweep_never_touches_anothers_tasks(conn, two_users):
    alice, bea = two_users
    bea_task = _task(conn, bea, _course(conn, bea), deadline="2020-01-01T00:00:00+00:00")

    repo.mark_overdue_tasks_as_missed(conn, user_id=alice, now="2026-09-01T00:00:00+00:00")

    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (bea_task,)).fetchone()
    assert row["status"] != "MISSED"


def test_source_side_cancellation_is_scoped_to_the_syncing_user(conn, two_users):
    """Alice's Classroom no longer lists the item; Bea's still does. A sync
    for Alice must not cancel Bea's copy."""
    alice, bea = two_users
    alice_course = _course(conn, alice)
    bea_course = _course(conn, bea)
    _task(conn, alice, alice_course)
    bea_task = _task(conn, bea, bea_course)

    cancelled = repo.cancel_tasks_missing_from_source(
        conn, user_id=alice, course_id=alice_course, source_type="coursework", seen_external_ids=set()
    )

    assert len(cancelled) == 1
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (bea_task,)).fetchone()
    assert row["status"] != "CANCELLED"


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


def test_an_identical_idempotency_key_does_not_suppress_the_other_users_reminder(conn, two_users):
    """The reason migration 0013 scoped the UNIQUE constraint. With a global
    constraint the second insert is silently ignored and that user simply
    never gets reminded - a missed deadline caused by a schema detail."""
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))
    bea_task = _task(conn, bea, _course(conn, bea))

    key = "shared-idempotency-key"
    assert repo.insert_reminder_if_absent(
        conn, user_id=alice, task_id=alice_task, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T12:00:00+00:00", idempotency_key=key,
    )
    assert repo.insert_reminder_if_absent(
        conn, user_id=bea, task_id=bea_task, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T12:00:00+00:00", idempotency_key=key,
    )

    total = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE idempotency_key = ?", (key,)
    ).fetchone()["c"]
    assert total == 2


def test_the_same_key_is_still_rejected_twice_for_one_user(conn, two_users):
    """Per-user scoping must not weaken idempotency within an account."""
    alice, _bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))

    key = "one-user-key"
    assert repo.insert_reminder_if_absent(
        conn, user_id=alice, task_id=alice_task, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T12:00:00+00:00", idempotency_key=key,
    )
    assert not repo.insert_reminder_if_absent(
        conn, user_id=alice, task_id=alice_task, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T12:00:00+00:00", idempotency_key=key,
    )


def test_due_reminders_are_never_drawn_from_another_users_pool(conn, two_users):
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))
    repo.insert_reminder_if_absent(
        conn, user_id=alice, task_id=alice_task, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-01T00:00:00+00:00", idempotency_key="alice-key",
    )

    assert len(repo.due_pending_reminders(conn, user_id=alice, now="2026-09-02T00:00:00+00:00")) == 1
    assert repo.due_pending_reminders(conn, user_id=bea, now="2026-09-02T00:00:00+00:00") == []


def test_cancelling_reminders_for_a_task_you_do_not_own_is_a_no_op(conn, two_users):
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))
    repo.insert_reminder_if_absent(
        conn, user_id=alice, task_id=alice_task, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-01T00:00:00+00:00", idempotency_key="alice-key",
    )

    repo.cancel_pending_reminders(conn, user_id=bea, task_id=alice_task)

    still_pending = conn.execute(
        "SELECT status FROM reminders WHERE task_id = ?", (alice_task,)
    ).fetchone()["status"]
    assert still_pending == "PENDING"


# ---------------------------------------------------------------------------
# Timetable and class reminders
# ---------------------------------------------------------------------------


def _timetable_event(conn, user_id, external_id="BSCS:2023:A:DLD:0"):
    return repo.upsert_timetable_event(
        conn,
        user_id=user_id,
        external_id=external_id,
        course_name="Digital Logic Design",
        program="BSCS",
        batch_year="2023",
        enrollment_type="REGULAR",
        day_of_week=0,
        occurrence_index=0,
        start_time="08:30",
        end_time="09:50",
        room="C-311",
        instructor=None,
        section="A",
        status="SCHEDULED",
        source_spreadsheet_id=None,
        source_sheet_gid=None,
        source_sheet_title="Monday",
    ).event_id


def test_two_users_can_sit_in_the_same_timetable_slot(conn, two_users):
    """Classmates derive the identical external_id from the same public
    sheet; migration 0015 is what stops the second one's row from
    colliding with the first's."""
    alice, bea = two_users
    assert _timetable_event(conn, alice) != _timetable_event(conn, bea)
    assert len(repo.list_timetable_events(conn, user_id=alice)) == 1
    assert len(repo.list_timetable_events(conn, user_id=bea)) == 1


def test_a_timetable_sync_for_one_user_does_not_cancel_anothers_classes(conn, two_users):
    alice, bea = two_users
    _timetable_event(conn, alice)
    bea_event = _timetable_event(conn, bea)

    cancelled = repo.cancel_timetable_events_missing_from_source(
        conn, user_id=alice, seen_external_ids=set()
    )

    assert len(cancelled) == 1
    row = conn.execute("SELECT status FROM timetable_events WHERE id = ?", (bea_event,)).fetchone()
    assert row["status"] == "SCHEDULED"


def test_class_reminders_are_claimed_per_user(conn, two_users):
    alice, bea = two_users
    alice_event = _timetable_event(conn, alice)
    bea_event = _timetable_event(conn, bea)

    assert repo.insert_class_reminder_if_absent(
        conn, user_id=alice, timetable_event_id=alice_event,
        occurrence_date="2026-09-07", reminder_type="CLASS_SOON",
        scheduled_for="2026-09-07T03:30:00+00:00",
    ) is not None
    assert repo.insert_class_reminder_if_absent(
        conn, user_id=bea, timetable_event_id=bea_event,
        occurrence_date="2026-09-07", reminder_type="CLASS_SOON",
        scheduled_for="2026-09-07T03:30:00+00:00",
    ) is not None

    assert len(repo.pending_class_reminders(conn, user_id=alice)) == 1
    assert len(repo.pending_class_reminders(conn, user_id=bea)) == 1


def test_expiring_stale_class_reminders_does_not_reach_across_users(conn, two_users):
    alice, bea = two_users
    _timetable_event(conn, alice)
    bea_event = _timetable_event(conn, bea)
    repo.insert_class_reminder_if_absent(
        conn, user_id=bea, timetable_event_id=bea_event,
        occurrence_date="2026-09-07", reminder_type="CLASS_SOON",
        scheduled_for="2026-09-07T03:30:00+00:00",
    )

    expired = repo.expire_stale_class_reminders(
        conn, user_id=alice, now="2026-09-08T00:00:00+00:00"
    )

    assert expired == 0
    assert len(repo.pending_class_reminders(conn, user_id=bea)) == 1


# ---------------------------------------------------------------------------
# Calendar events, deliveries, sync state, health
# ---------------------------------------------------------------------------


def test_two_users_can_own_the_same_google_event_id(conn, two_users):
    """Not hypothetical: a shared or duplicated calendar entry, or simply an
    id collision across accounts, must not make the second write fail."""
    alice, bea = two_users
    alice_task = _task(conn, alice, _course(conn, alice))
    bea_task = _task(conn, bea, _course(conn, bea))

    repo.record_calendar_event(
        conn, user_id=alice, task_id=alice_task, kind="ACTUAL_DEADLINE", event_id="google-evt-1"
    )
    repo.record_calendar_event(
        conn, user_id=bea, task_id=bea_task, kind="ACTUAL_DEADLINE", event_id="google-evt-1"
    )

    assert repo.get_calendar_event(
        conn, user_id=alice, task_id=alice_task, kind="ACTUAL_DEADLINE"
    ) is not None
    assert repo.get_calendar_event(
        conn, user_id=bea, task_id=alice_task, kind="ACTUAL_DEADLINE"
    ) is None


def test_delivery_history_is_never_shown_across_accounts(conn, two_users):
    """The /deliveries page renders these rows, so an unscoped read would
    put one user's notification log on another user's screen."""
    alice, bea = two_users
    repo.record_notification_delivery(conn, user_id=alice, provider="FakeProvider", ok=True)

    assert len(repo.recent_notification_deliveries(conn, user_id=alice)) == 1
    assert repo.recent_notification_deliveries(conn, user_id=bea) == []


def test_sync_state_is_tracked_separately_per_user(conn, two_users):
    """With a single row per source, one user's success would mask another's
    failure - and the failing account would look healthy."""
    alice, bea = two_users
    repo.record_sync_start(conn, user_id=alice, source="classroom")
    repo.record_sync_start(conn, user_id=bea, source="classroom")
    repo.record_sync_error(conn, user_id=bea, source="classroom", error="token expired")
    repo.record_sync_success(conn, user_id=alice, source="classroom")

    states = {
        row["user_id"]: row["status"]
        for row in conn.execute("SELECT user_id, status FROM sync_state WHERE source = 'classroom'")
    }
    assert states[alice] == "OK"
    assert states[bea] == "ERROR"


def test_a_healthy_run_does_not_reset_another_users_failure_streak(conn, two_users):
    """The sharpest consequence of a shared table: the self-alert fires
    after three consecutive failures, so a neighbour's success resetting the
    streak would suppress the alert for a genuinely broken account
    indefinitely."""
    from ragra import health

    alice, bea = two_users
    for _ in range(3):
        streak = health.record_result(conn, user_id=bea, component="classroom", success=False, error="boom")
    assert streak == 3

    health.record_result(conn, user_id=alice, component="classroom", success=True)

    still_failing = conn.execute(
        "SELECT consecutive_failures FROM pipeline_health WHERE user_id = ? AND component = 'classroom'",
        (bea,),
    ).fetchone()["consecutive_failures"]
    assert still_failing == 3


def test_a_health_alert_is_raised_only_for_the_user_that_is_failing(conn, two_users):
    from ragra import health
    from ragra.adapters.notify import NotifyResult

    class RecordingProvider:
        def __init__(self):
            self.sent = []

        def send(self, notification):
            self.sent.append(notification.text)
            return NotifyResult(ok=True)

    alice, bea = two_users
    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, user_id=bea, component="classroom", success=False, error="boom")

    alice_provider = RecordingProvider()
    assert health.check_and_alert(conn, user_id=alice, providers=[alice_provider]) == []
    assert alice_provider.sent == []

    bea_provider = RecordingProvider()
    assert health.check_and_alert(conn, user_id=bea, providers=[bea_provider]) == ["classroom"]
    assert len(bea_provider.sent) == 1


# ---------------------------------------------------------------------------
# Personal pseudo-course
# ---------------------------------------------------------------------------


def test_each_user_gets_their_own_personal_pseudo_course(conn, two_users):
    """Manual tasks hang off this course, so sharing it across accounts
    would put both users' personal work under one parent."""
    alice, bea = two_users
    assert repo.personal_course_id(conn, user_id=alice) != repo.personal_course_id(conn, user_id=bea)


def test_the_personal_pseudo_course_is_stable_for_a_given_user(conn, two_users):
    alice, _bea = two_users
    assert repo.personal_course_id(conn, user_id=alice) == repo.personal_course_id(conn, user_id=alice)


# ---------------------------------------------------------------------------
# Cascade completeness (the foundation P3-11 account deletion builds on)
# ---------------------------------------------------------------------------


def test_deleting_a_user_removes_every_row_they_owned(conn, two_users):
    """Not the account-deletion feature itself (P3-11) - this asserts the
    schema-level cascade it will rely on, so a table added without
    ON DELETE CASCADE is caught here rather than as orphaned data later."""
    alice, bea = two_users
    bea_course = _course(conn, bea)
    bea_task = _task(conn, bea, bea_course)
    repo.insert_reminder_if_absent(
        conn, user_id=bea, task_id=bea_task, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T12:00:00+00:00", idempotency_key="bea-key",
    )
    repo.record_notification_delivery(conn, user_id=bea, provider="FakeProvider", ok=True)
    repo.record_sync_start(conn, user_id=bea, source="classroom")
    _timetable_event(conn, bea)
    alice_task = _task(conn, alice, _course(conn, alice))

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users WHERE id = ?", (bea,))
    conn.commit()

    for table in (
        "courses", "tasks", "task_history", "reminders", "calendar_events",
        "sync_state", "pipeline_health", "tick_sessions", "timetable_events",
        "class_reminders", "notification_deliveries",
    ):
        remaining = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (bea,)
        ).fetchone()["c"]
        assert remaining == 0, f"{table} still holds rows for the deleted user"

    # Alice is untouched, which is the other half of the property.
    assert repo.get_task_by_id(conn, user_id=alice, task_id=alice_task) is not None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
