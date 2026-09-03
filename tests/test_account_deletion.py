"""Account deletion, and specifically its completeness.

The failure this suite is written against is not a crash. It is a deletion
that reports success and leaves something behind - a session that still
works, a Google grant Ragra can still use, a notification destination still
receiving reminders. Each of those is worse than refusing to delete, because
the user has been told it is gone.

So the central test does not check a list of tables somebody remembered to
write down. It walks the schema, populates every table that carries a
user_id, and asserts each one is empty afterwards. A table added next year
without ON DELETE CASCADE fails that test on the day it is added.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ragra import accounts, crypto
from ragra.adapters import google_credentials as gc
from ragra.db import repo
from ragra.notifications.preferences import NotificationPreferences, save_preferences
from ragra.relevance.profile import save_profile
from ragra.timetable.enrollment import REGULAR, EnrolledCourse
from ragra.web import sessions
from tests.support import make_user, owner_id

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def key() -> bytes:
    return crypto.load_key({crypto.KEY_ENV_VAR: crypto.generate_key()})


@pytest.fixture
def alice(conn) -> int:
    return owner_id(conn)


@pytest.fixture
def bea(conn) -> int:
    return make_user(conn, google_sub="deletion-second", display_name="Bea")


def _populate(conn, user_id: int, key: bytes, *, label: str) -> dict:
    """Give a user at least one row in every table that carries a user_id.

    Returns the ids the tests need. The completeness test below verifies
    against the schema that this really did cover everything, so a new
    table cannot be silently left unpopulated and therefore untested.
    """
    course_id = repo.upsert_course(
        conn, user_id=user_id, external_id=f"course-{label}", name="Digital Logic",
        section="A", teacher=None, course_code="EE1005", state="ACTIVE",
    )
    task_id = repo.upsert_task_from_source(
        conn, user_id=user_id, course_id=course_id, source_type="coursework",
        external_id=f"item-{label}", title="Lab 3", description=None, link=None,
        kind="ACTIONABLE", actual_deadline="2026-09-10T12:00:00+00:00",
        source_published_at=None, source_updated_at=None,
    ).task_id
    repo.insert_reminder_if_absent(
        conn, user_id=user_id, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T12:00:00+00:00", idempotency_key=f"key-{label}",
    )
    repo.record_calendar_event(
        conn, user_id=user_id, task_id=task_id, kind="ACTUAL_DEADLINE",
        event_id=f"google-evt-{label}",
    )
    repo.record_sync_start(conn, user_id=user_id, source="classroom")
    repo.record_notification_delivery(conn, user_id=user_id, provider="FakeProvider", ok=True)
    repo.record_tick_session(
        conn, user_id=user_id, started_at="2026-09-01T00:00:00+00:00",
        finished_at="2026-09-01T00:00:05+00:00", duration_seconds=5.0, exit_code=0,
        classroom_result="ok", calendar_result="ok", reminders_result="ok",
        timetable_result="ok", error=None,
    )
    event_id = repo.upsert_timetable_event(
        conn, user_id=user_id, external_id=f"tt-{label}", course_name="Digital Logic",
        program="BSCS", batch_year="2023", enrollment_type="REGULAR", day_of_week=0,
        occurrence_index=0, start_time="08:30", end_time="09:50", room="C-311",
        instructor=None, section="A", status="SCHEDULED", source_spreadsheet_id=None,
        source_sheet_gid=None, source_sheet_title="Monday",
    ).event_id
    repo.insert_class_reminder_if_absent(
        conn, user_id=user_id, timetable_event_id=event_id, occurrence_date="2026-09-07",
        reminder_type="CLASS_SOON", scheduled_for="2026-09-07T03:30:00+00:00",
    )

    from ragra import health

    health.record_result(conn, user_id=user_id, component="classroom", success=False, error="x")

    token = sessions.create_session(conn, user_id=user_id, now=NOW)
    gc.store(conn, user_id=user_id, service=gc.CLASSROOM, payload='{"token": "x"}', key=key)
    save_profile(
        conn, user_id=user_id, program="CS", batch_year="2025",
        enrollment_start_year=2025, enrollment_start_term="FALL",
        enrollment=(EnrolledCourse("Digital Logic", "CS-G", REGULAR),),
    )
    save_preferences(
        conn, user_id=user_id,
        preferences=NotificationPreferences(email_enabled=True, email_to=f"{label}@example.com"),
    )
    conn.commit()
    return {"task_id": task_id, "session_token": token, "course_id": course_id}


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


def test_the_fixture_really_covers_every_owned_table(conn, key, alice):
    """Guards the test below. If _populate stopped filling a table, the
    completeness check would pass while proving nothing about it."""
    _populate(conn, alice, key, label="alice")

    empty = [
        table
        for table in accounts.owned_tables(conn)
        if conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (alice,)
        ).fetchone()["c"]
        == 0
    ]
    assert not empty, f"the deletion fixture leaves these tables empty: {empty}"


def test_deleting_an_account_empties_every_table_that_carries_a_user_id(conn, key, alice, bea):
    """Walks the schema rather than a written-down list, so a table added
    without ON DELETE CASCADE fails here on the day it is added."""
    _populate(conn, bea, key, label="bea")
    _populate(conn, alice, key, label="alice")

    accounts.delete_account(conn, user_id=bea)

    for table in accounts.owned_tables(conn):
        remaining = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (bea,)
        ).fetchone()["c"]
        assert remaining == 0, f"{table} still holds rows for the deleted account"

    assert repo.get_user(conn, user_id=bea) is None


def test_deleting_one_account_leaves_the_others_completely_intact(conn, key, alice, bea):
    alice_ids = _populate(conn, alice, key, label="alice")
    _populate(conn, bea, key, label="bea")

    before = {
        table: conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (alice,)
        ).fetchone()["c"]
        for table in accounts.owned_tables(conn)
    }

    accounts.delete_account(conn, user_id=bea)

    after = {
        table: conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (alice,)
        ).fetchone()["c"]
        for table in accounts.owned_tables(conn)
    }
    assert after == before
    assert repo.get_task_by_id(conn, user_id=alice, task_id=alice_ids["task_id"]) is not None


def test_no_foreign_key_violation_is_left_behind(conn, key, alice, bea):
    """Orphaned rows are the specific failure a cascade is meant to prevent,
    and the one that would go unnoticed - nothing in the product shows a row
    whose owner is gone."""
    _populate(conn, bea, key, label="bea")
    _populate(conn, alice, key, label="alice")

    accounts.delete_account(conn, user_id=bea)

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---------------------------------------------------------------------------
# The things that must stop working
# ---------------------------------------------------------------------------


def test_a_deleted_accounts_sessions_stop_working_immediately(conn, key, bea):
    """A browser holding a live session for an account that no longer
    exists is the clearest possible failure of "deleted"."""
    ids = _populate(conn, bea, key, label="bea")

    accounts.delete_account(conn, user_id=bea)

    assert sessions.lookup_session(conn, token=ids["session_token"], now=NOW) is None


def test_a_deleted_accounts_google_authorization_is_destroyed(conn, key, bea):
    """Otherwise a deleted account leaves a grant Ragra can still use, with
    nothing in the product showing it."""
    _populate(conn, bea, key, label="bea")

    accounts.delete_account(conn, user_id=bea)

    assert gc.has_credentials(conn, user_id=bea, service=gc.CLASSROOM) is False


def test_a_deleted_account_has_no_notification_destination_left(conn, key, bea):
    """The most visible leftover: reminders continuing to arrive for work
    nobody can see any more."""
    _populate(conn, bea, key, label="bea")

    accounts.delete_account(conn, user_id=bea)

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM notification_preferences WHERE user_id = ?", (bea,)
    ).fetchone()["c"] == 0


def test_a_deleted_account_is_no_longer_processed_by_the_tick(conn, key, alice, bea):
    _populate(conn, bea, key, label="bea")

    accounts.delete_account(conn, user_id=bea)

    assert [row["id"] for row in repo.list_users(conn)] == [alice]


def test_a_deleted_accounts_google_subject_can_sign_in_again_as_a_new_account(conn, key, bea):
    """Deletion must not blacklist the person. Signing in again gives a
    fresh, empty account - not the deleted one back."""
    conn.execute("UPDATE users SET google_sub = 'reusable-sub' WHERE id = ?", (bea,))
    conn.commit()
    _populate(conn, bea, key, label="bea")

    accounts.delete_account(conn, user_id=bea)

    assert repo.get_user_by_google_sub(conn, google_sub="reusable-sub") is None
    replacement = make_user(conn, google_sub="reusable-sub", display_name="Bea again")
    assert replacement != bea
    assert repo.manual_tasks(conn, user_id=replacement) == []


# ---------------------------------------------------------------------------
# Safety and reporting
# ---------------------------------------------------------------------------


def test_deleting_an_account_that_does_not_exist_is_reported_not_swallowed(conn):
    """"Deleted" and "was never here" are different answers, and only one
    of them means the caller's request was carried out."""
    with pytest.raises(accounts.UnknownAccount):
        accounts.delete_account(conn, user_id=99999)


def test_a_preview_removes_nothing(conn, key, bea):
    _populate(conn, bea, key, label="bea")

    summary = accounts.preview_deletion(conn, user_id=bea)

    assert summary.total_rows > 0
    assert repo.get_user(conn, user_id=bea) is not None
    assert sessions.active_sessions_for_user(conn, user_id=bea) == 1


def test_the_preview_counts_what_deletion_actually_removes(conn, key, alice, bea):
    """The confirmation a user sees is generated from the same schema walk
    the deletion uses, so it cannot quietly stop being true."""
    _populate(conn, bea, key, label="bea")
    _populate(conn, alice, key, label="alice")

    summary = accounts.preview_deletion(conn, user_id=bea)
    counted = dict(summary.rows_by_table)

    accounts.delete_account(conn, user_id=bea)

    for table, expected in counted.items():
        assert expected > 0
        assert conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (bea,)
        ).fetchone()["c"] == 0


def test_deletion_refuses_to_run_with_foreign_keys_disabled(conn, key, bea):
    """With the pragma off, `DELETE FROM users` succeeds and silently
    orphans everything - a deletion that looks successful and is not."""
    _populate(conn, bea, key, label="bea")
    conn.execute("PRAGMA foreign_keys=OFF")

    with pytest.raises(RuntimeError, match="foreign keys disabled"):
        repo.delete_user(conn, user_id=bea)

    assert repo.get_user(conn, user_id=bea) is not None


def test_the_receipt_says_plainly_what_deletion_does_not_do(conn, key, bea):
    """A user who believes their Google access was revoked when it was only
    forgotten locally has been misled - a product failure even though no
    code is wrong."""
    _populate(conn, bea, key, label="bea")

    text = "\n".join(accounts.describe(accounts.preview_deletion(conn, user_id=bea)))

    assert "does not withdraw the grant" in text
    assert "myaccount.google.com/permissions" in text


def test_the_receipt_names_the_tables_it_will_clear(conn, key, bea):
    _populate(conn, bea, key, label="bea")

    text = "\n".join(accounts.describe(accounts.preview_deletion(conn, user_id=bea)))

    assert "tasks:" in text
    assert "reminders:" in text
    assert "sign-in session(s) will stop working immediately" in text


def test_an_account_with_nothing_stored_deletes_cleanly(conn, bea):
    """The empty case must not look like an error - a user who never used
    Ragra is still entitled to delete their account."""
    summary = accounts.delete_account(conn, user_id=bea)

    assert summary.total_rows == 0
    assert repo.get_user(conn, user_id=bea) is None
    assert "no stored data" in "\n".join(accounts.describe(summary))
