from ragra.db import repo

from tests.support import owner_id


def _make_course(conn):
    return repo.upsert_course(
        conn,
        external_id="course-1",
        name="Object Oriented Programming",
        section="BCS-3C",
        teacher="Dr. Smith",
        course_code="CS1004",
        state="ACTIVE", user_id=owner_id(conn),
    )


def test_repeated_course_sync_does_not_duplicate(conn):
    id1 = _make_course(conn)
    id2 = _make_course(conn)
    assert id1 == id2
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM courses WHERE external_id = 'course-1'"
    ).fetchone()["c"]
    assert count == 1


def test_repeated_task_sync_does_not_duplicate(conn):
    course_id = _make_course(conn)
    kwargs = dict(
        course_id=course_id,
        source_type="coursework",
        external_id="cw-1",
        title="Assignment 2",
        description="Do the thing",
        link="https://classroom.google.com/x",
        kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00",
    )

    r1 = repo.upsert_task_from_source(conn, **kwargs, user_id=owner_id(conn))
    r2 = repo.upsert_task_from_source(conn, **kwargs, user_id=owner_id(conn))
    r3 = repo.upsert_task_from_source(conn, **kwargs, user_id=owner_id(conn))

    assert r1.created is True
    assert r2.created is False
    assert r3.created is False
    assert r1.task_id == r2.task_id == r3.task_id

    count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert count == 1


def test_deadline_change_updates_existing_task_and_preserves_history(conn):
    course_id = _make_course(conn)
    base = dict(
        course_id=course_id,
        source_type="coursework",
        external_id="cw-2",
        title="Assignment 3",
        description="desc",
        link=None,
        kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00",
    )
    created = repo.upsert_task_from_source(conn, **base, user_id=owner_id(conn))
    assert created.created is True

    changed = dict(base)
    changed["actual_deadline"] = "2026-09-13T23:59:00+00:00"
    changed["source_updated_at"] = "2026-08-25T00:00:00+00:00"
    result = repo.upsert_task_from_source(conn, **changed, user_id=owner_id(conn))

    assert result.created is False
    assert result.task_id == created.task_id
    assert result.deadline_changed is True
    assert result.old_deadline == "2026-09-10T23:59:00+00:00"
    assert result.new_deadline == "2026-09-13T23:59:00+00:00"

    # Only one task row still exists.
    count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert count == 1

    row = conn.execute("SELECT actual_deadline FROM tasks WHERE id = ?", (created.task_id,)).fetchone()
    assert row["actual_deadline"] == "2026-09-13T23:59:00+00:00"

    history = conn.execute(
        "SELECT * FROM task_history WHERE task_id = ? AND field = 'actual_deadline'",
        (created.task_id,),
    ).fetchall()
    assert len(history) == 1
    assert history[0]["old_value"] == "2026-09-10T23:59:00+00:00"
    assert history[0]["new_value"] == "2026-09-13T23:59:00+00:00"


def test_completed_task_receives_no_future_reminders(conn):
    course_id = _make_course(conn)
    result = repo.upsert_task_from_source(
        conn,
        course_id=course_id,
        source_type="coursework",
        external_id="cw-3",
        title="Assignment 4",
        description=None,
        link=None,
        kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    task_id = result.task_id

    repo.insert_reminder_if_absent(
        conn, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T08:00:00+00:00", idempotency_key="k1", user_id=owner_id(conn),
    )

    repo.mark_completed(conn, task_id=task_id, user_id=owner_id(conn))
    repo.cancel_pending_reminders(conn, task_id=task_id, user_id=owner_id(conn))

    due = repo.due_pending_reminders(conn, now="2026-12-01T00:00:00+00:00", user_id=owner_id(conn))
    assert due == []


def test_missed_task_is_detected(conn):
    course_id = _make_course(conn)
    result = repo.upsert_task_from_source(
        conn,
        course_id=course_id,
        source_type="coursework",
        external_id="cw-4",
        title="Assignment 5",
        description=None,
        link=None,
        kind="ACTIONABLE",
        actual_deadline="2020-01-01T00:00:00+00:00",
        source_published_at="2019-12-01T00:00:00+00:00",
        source_updated_at="2019-12-01T00:00:00+00:00", user_id=owner_id(conn),
    )
    overdue = repo.overdue_tasks(conn, now="2026-01-01T00:00:00+00:00", user_id=owner_id(conn))
    assert len(overdue) == 1
    assert overdue[0]["id"] == result.task_id

    repo.mark_missed(conn, task_id=result.task_id, user_id=owner_id(conn))
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    assert row["status"] == "MISSED"

    # Missing again is idempotent / does not error and does not clobber a
    # later completion.
    repo.mark_completed(conn, task_id=result.task_id, user_id=owner_id(conn))
    row = conn.execute("SELECT status, missed_at, completed_at FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    assert row["status"] == "COMPLETED"
    assert row["missed_at"] is not None  # history preserved, not erased
    assert row["completed_at"] is not None


def test_reminder_insert_is_idempotent_on_same_key(conn):
    course_id = _make_course(conn)
    result = repo.upsert_task_from_source(
        conn,
        course_id=course_id,
        source_type="coursework",
        external_id="cw-5",
        title="Assignment 6",
        description=None,
        link=None,
        kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    task_id = result.task_id

    inserted1 = repo.insert_reminder_if_absent(
        conn, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T08:00:00+00:00", idempotency_key="same-key", user_id=owner_id(conn),
    )
    inserted2 = repo.insert_reminder_if_absent(
        conn, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T08:00:00+00:00", idempotency_key="same-key", user_id=owner_id(conn),
    )
    assert inserted1 is True
    assert inserted2 is False

    count = conn.execute("SELECT COUNT(*) AS c FROM reminders").fetchone()["c"]
    assert count == 1


def test_tasks_missing_personal_target(conn):
    course_id = _make_course(conn)
    with_deadline = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-a",
        title="Has deadline, no personal target", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00", source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-b",
        title="No deadline at all", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=None,
        source_published_at="2026-08-20T00:00:00+00:00", source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    already_planned = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-c",
        title="Has deadline AND personal target", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-09-11T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00", source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    repo.set_personal_deadline(conn, task_id=already_planned.task_id, personal_deadline="2026-09-08", user_id=owner_id(conn))

    rows = repo.tasks_missing_personal_target(conn, user_id=owner_id(conn))
    ids = {r["id"] for r in rows}
    assert ids == {with_deadline.task_id}


def test_deadline_change_cancels_and_recomputes_reminders(conn):
    course_id = _make_course(conn)
    result = repo.upsert_task_from_source(
        conn,
        course_id=course_id,
        source_type="coursework",
        external_id="cw-6",
        title="Assignment 7",
        description=None,
        link=None,
        kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    task_id = result.task_id
    repo.insert_reminder_if_absent(
        conn, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-09T08:00:00+00:00",
        idempotency_key=f"{task_id}:T_MINUS_1D:2026-09-10T23:59:00+00:00", user_id=owner_id(conn),
    )

    # Deadline moves; caller (sync engine) is responsible for calling this.
    repo.cancel_pending_reminders(conn, task_id=task_id, user_id=owner_id(conn))
    repo.insert_reminder_if_absent(
        conn, task_id=task_id, reminder_type="T_MINUS_1D",
        scheduled_for="2026-09-12T08:00:00+00:00",
        idempotency_key=f"{task_id}:T_MINUS_1D:2026-09-13T23:59:00+00:00", user_id=owner_id(conn),
    )

    rows = conn.execute("SELECT status FROM reminders WHERE task_id = ?", (task_id,)).fetchall()
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["CANCELLED", "PENDING"]
