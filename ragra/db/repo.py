"""Data access layer. Every write here is idempotent by design: re-running a
sync or reminder pass must never duplicate a course, task, history entry, or
reminder.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------


def upsert_course(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    name: str,
    section: str | None,
    teacher: str | None,
    course_code: str | None,
    state: str,
) -> int:
    now = now_iso()
    row = conn.execute(
        "SELECT id FROM courses WHERE external_id = ?", (external_id,)
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """INSERT INTO courses
               (external_id, course_code, name, section, teacher, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (external_id, course_code, name, section, teacher, state, now, now),
        )
        conn.commit()
        return cur.lastrowid

    conn.execute(
        """UPDATE courses
           SET course_code = ?, name = ?, section = ?, teacher = ?, state = ?, updated_at = ?
           WHERE id = ?""",
        (course_code, name, section, teacher, state, now, row["id"]),
    )
    conn.commit()
    return row["id"]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@dataclass
class TaskUpsertResult:
    task_id: int
    created: bool
    deadline_changed: bool = False
    old_deadline: str | None = None
    new_deadline: str | None = None
    other_fields_changed: list[str] = field(default_factory=list)


_TRACKED_FIELDS = ("title", "description", "link", "actual_deadline")


def get_task_by_source(
    conn: sqlite3.Connection, *, course_id: int, source_type: str, external_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM tasks
           WHERE course_id = ? AND source_type = ? AND external_id = ?""",
        (course_id, source_type, external_id),
    ).fetchone()


def upsert_task_from_source(
    conn: sqlite3.Connection,
    *,
    course_id: int,
    source_type: str,
    external_id: str,
    title: str,
    description: str | None,
    link: str | None,
    kind: str,
    actual_deadline: str | None,
    source_published_at: str | None,
    source_updated_at: str | None,
) -> TaskUpsertResult:
    now = now_iso()
    existing = get_task_by_source(
        conn, course_id=course_id, source_type=source_type, external_id=external_id
    )

    if existing is None:
        initial_status = "ACTION_REQUIRED" if kind == "ACTIONABLE" else "DISCOVERED"
        cur = conn.execute(
            """INSERT INTO tasks
               (course_id, source_type, external_id, title, description, link, kind, status,
                actual_deadline, personal_deadline, source_published_at, source_updated_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
            (
                course_id, source_type, external_id, title, description, link, kind,
                initial_status, actual_deadline, source_published_at, source_updated_at,
                now, now,
            ),
        )
        task_id = cur.lastrowid
        record_history(conn, task_id=task_id, field_name="created", old_value=None, new_value=title)
        conn.commit()
        return TaskUpsertResult(task_id=task_id, created=True)

    task_id = existing["id"]

    # Short-circuit: if the source hasn't changed since we last saw it, skip
    # comparison entirely. Classroom's updateTime is the cheap idempotency
    # signal for "nothing to reconcile" on a routine poll.
    if source_updated_at is not None and source_updated_at == existing["source_updated_at"]:
        return TaskUpsertResult(task_id=task_id, created=False)

    new_values = {
        "title": title,
        "description": description,
        "link": link,
        "actual_deadline": actual_deadline,
    }
    changed: list[str] = []
    deadline_changed = False
    old_deadline = existing["actual_deadline"]
    for field_name in _TRACKED_FIELDS:
        old_value = existing[field_name]
        new_value = new_values[field_name]
        if old_value != new_value:
            changed.append(field_name)
            record_history(
                conn, task_id=task_id, field_name=field_name, old_value=old_value, new_value=new_value
            )
            if field_name == "actual_deadline":
                deadline_changed = True

    conn.execute(
        """UPDATE tasks
           SET title = ?, description = ?, link = ?, actual_deadline = ?,
               source_updated_at = ?, updated_at = ?
           WHERE id = ?""",
        (title, description, link, actual_deadline, source_updated_at, now, task_id),
    )
    conn.commit()

    return TaskUpsertResult(
        task_id=task_id,
        created=False,
        deadline_changed=deadline_changed,
        old_deadline=old_deadline if deadline_changed else None,
        new_deadline=actual_deadline if deadline_changed else None,
        other_fields_changed=[f for f in changed if f != "actual_deadline"],
    )


def record_history(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
) -> None:
    conn.execute(
        """INSERT INTO task_history (task_id, changed_at, field, old_value, new_value)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, now_iso(), field_name, old_value, new_value),
    )


def set_personal_deadline(conn: sqlite3.Connection, *, task_id: int, personal_deadline: str) -> None:
    row = conn.execute("SELECT personal_deadline FROM tasks WHERE id = ?", (task_id,)).fetchone()
    old_value = row["personal_deadline"] if row else None
    conn.execute(
        "UPDATE tasks SET personal_deadline = ?, status = CASE WHEN status = 'ACTION_REQUIRED' THEN 'PLANNED' ELSE status END, updated_at = ? WHERE id = ?",
        (personal_deadline, now_iso(), task_id),
    )
    record_history(conn, task_id=task_id, field_name="personal_deadline", old_value=old_value, new_value=personal_deadline)
    conn.commit()


def mark_completed(conn: sqlite3.Connection, *, task_id: int) -> None:
    now = now_iso()
    conn.execute(
        "UPDATE tasks SET status = 'COMPLETED', completed_at = ?, updated_at = ? WHERE id = ?",
        (now, now, task_id),
    )
    record_history(conn, task_id=task_id, field_name="status", old_value=None, new_value="COMPLETED")
    conn.commit()


def mark_missed(conn: sqlite3.Connection, *, task_id: int) -> None:
    now = now_iso()
    conn.execute(
        "UPDATE tasks SET status = 'MISSED', missed_at = ?, updated_at = ? WHERE id = ? AND status NOT IN ('COMPLETED', 'CANCELLED')",
        (now, now, task_id),
    )
    record_history(conn, task_id=task_id, field_name="status", old_value=None, new_value="MISSED")
    conn.commit()


def mark_overdue_tasks_as_missed(conn: sqlite3.Connection, *, now: str) -> list[int]:
    """Reconciliation pass: any task past its actual_deadline that isn't
    completed, cancelled, or already missed transitions to MISSED. Excludes
    already-MISSED tasks explicitly (not just relying on mark_missed's own
    guard) so repeated calls - every sync/tick - never re-touch missed_at or
    append duplicate history rows for a task already marked missed."""
    rows = conn.execute(
        """SELECT id FROM tasks
           WHERE actual_deadline IS NOT NULL AND actual_deadline < ?
           AND status NOT IN ('COMPLETED', 'CANCELLED', 'MISSED')""",
        (now,),
    ).fetchall()
    missed_task_ids = []
    for row in rows:
        mark_missed(conn, task_id=row["id"])
        missed_task_ids.append(row["id"])
    return missed_task_ids


def cancel_task(conn: sqlite3.Connection, *, task_id: int) -> None:
    now = now_iso()
    conn.execute(
        "UPDATE tasks SET status = 'CANCELLED', cancelled_at = ?, updated_at = ? WHERE id = ?",
        (now, now, task_id),
    )
    record_history(conn, task_id=task_id, field_name="status", old_value=None, new_value="CANCELLED")
    conn.commit()


def cancel_tasks_missing_from_source(
    conn: sqlite3.Connection, *, course_id: int, source_type: str, seen_external_ids: set[str]
) -> list[int]:
    """A Classroom sync pass that no longer sees a previously-discovered
    coursework/announcement/material item (deleted or unpublished at the
    source) cancels the matching Ragra task and its pending reminders.
    Completed/already-cancelled tasks are left alone - history is never
    erased by a source-side removal."""
    rows = conn.execute(
        """SELECT id, external_id FROM tasks
           WHERE course_id = ? AND source_type = ? AND status NOT IN ('COMPLETED', 'CANCELLED')""",
        (course_id, source_type),
    ).fetchall()
    cancelled_task_ids = []
    for row in rows:
        if row["external_id"] not in seen_external_ids:
            cancel_task(conn, task_id=row["id"])
            cancel_pending_reminders(conn, task_id=row["id"])
            cancelled_task_ids.append(row["id"])
    return cancelled_task_ids


def tasks_due_between(conn: sqlite3.Connection, *, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT tasks.*, courses.course_code, courses.name AS course_name
           FROM tasks JOIN courses ON courses.id = tasks.course_id
           WHERE actual_deadline IS NOT NULL AND actual_deadline BETWEEN ? AND ?
           AND status NOT IN ('COMPLETED', 'CANCELLED')
           ORDER BY actual_deadline ASC""",
        (start_iso, end_iso),
    ).fetchall()


def tasks_missing_personal_target(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Actionable tasks that already have an authoritative actual_deadline
    from Classroom but no personal completion target yet - Ragra should
    surface these so the developer can decide when HE plans to actually do the
    work, independent of (and possibly earlier than) the academic deadline."""
    return conn.execute(
        """SELECT tasks.*, courses.course_code, courses.name AS course_name
           FROM tasks JOIN courses ON courses.id = tasks.course_id
           WHERE kind = 'ACTIONABLE' AND actual_deadline IS NOT NULL
           AND personal_deadline IS NULL AND status NOT IN ('COMPLETED', 'CANCELLED', 'MISSED')
           ORDER BY actual_deadline ASC"""
    ).fetchall()


def overdue_tasks(conn: sqlite3.Connection, *, now: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT tasks.*, courses.course_code, courses.name AS course_name
           FROM tasks JOIN courses ON courses.id = tasks.course_id
           WHERE actual_deadline IS NOT NULL AND actual_deadline < ?
           AND status NOT IN ('COMPLETED', 'CANCELLED', 'MISSED')
           ORDER BY actual_deadline ASC""",
        (now,),
    ).fetchall()


def missed_tasks(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    """Tasks whose deadline passed without completion (status MISSED - see
    repo.mark_overdue_tasks_as_missed). Distinct from overdue_tasks(): an
    OVERDUE task is still actionable/pending; MISSED is the terminal state
    a task transitions into once genuinely past due. Neither query overlaps
    the other by construction (overdue_tasks excludes MISSED).

    Ordered by actual_deadline DESC (most recently due first) - not
    missed_at, which only records when Ragra's reconciliation pass noticed
    it and would cluster everything from the same sync run together
    regardless of how old the underlying deadline actually is. limit=None
    (the default) returns everything; pass a limit for a "most recent"
    slice, e.g. for a dashboard summary that shouldn't be dominated by a
    long tail of old items - see count_missed_tasks() for the total."""
    query = """SELECT tasks.*, courses.course_code, courses.name AS course_name
               FROM tasks JOIN courses ON courses.id = tasks.course_id
               WHERE status = 'MISSED'
               ORDER BY actual_deadline DESC"""
    if limit is None:
        return conn.execute(query).fetchall()
    return conn.execute(query + " LIMIT ?", (limit,)).fetchall()


def count_missed_tasks(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'MISSED'").fetchone()["c"]


def get_task_by_id(conn: sqlite3.Connection, *, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT tasks.*, courses.course_code, courses.name AS course_name
           FROM tasks JOIN courses ON courses.id = tasks.course_id
           WHERE tasks.id = ?""",
        (task_id,),
    ).fetchone()


def reminders_for_task(conn: sqlite3.Connection, *, task_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM reminders WHERE task_id = ?
           ORDER BY scheduled_for ASC""",
        (task_id,),
    ).fetchall()


def history_for_task(conn: sqlite3.Connection, *, task_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM task_history WHERE task_id = ?
           ORDER BY changed_at DESC""",
        (task_id,),
    ).fetchall()


def recently_completed_tasks(conn: sqlite3.Connection, *, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT tasks.*, courses.course_code, courses.name AS course_name
           FROM tasks JOIN courses ON courses.id = tasks.course_id
           WHERE status = 'COMPLETED'
           ORDER BY completed_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def upcoming_scheduled_reminders(conn: sqlite3.Connection, *, now: str, limit: int = 15) -> list[sqlite3.Row]:
    """PENDING reminders not yet due - "what's coming" for the dashboard,
    distinct from due_pending_reminders (dispatch's "what's due right now")."""
    return conn.execute(
        """SELECT reminders.*, tasks.title AS task_title,
                  COALESCE(courses.course_code, courses.name) AS course_code
           FROM reminders
           JOIN tasks ON tasks.id = reminders.task_id
           JOIN courses ON courses.id = tasks.course_id
           WHERE reminders.status = 'PENDING' AND reminders.scheduled_for > ?
           ORDER BY reminders.scheduled_for ASC LIMIT ?""",
        (now, limit),
    ).fetchall()


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


def cancel_pending_reminders(conn: sqlite3.Connection, *, task_id: int) -> None:
    conn.execute(
        "UPDATE reminders SET status = 'CANCELLED' WHERE task_id = ? AND status = 'PENDING'",
        (task_id,),
    )
    conn.commit()


def cancel_backlog_reminders_for_already_overdue_tasks(conn: sqlite3.Connection) -> int:
    """Safety net, not a one-off migration: a task whose actual_deadline was
    already at-or-before the moment Ragra first discovered it (its own
    created_at) must never carry pending pre-deadline reminders - those
    reminder windows never meaningfully existed for Ragra to fire. Idempotent
    (cancelling an already-cancelled reminder is a no-op) and safe to run on
    every sync, so it also self-heals data written under an older, buggy
    scheduling rule and covers any future re-import of old material (e.g. an
    archived course reactivated later). Only cancels PENDING rows - SENT
    history is never touched or deleted. Returns the number of reminder rows
    actually cancelled."""
    rows = conn.execute(
        """SELECT tasks.id FROM tasks
           WHERE actual_deadline IS NOT NULL AND actual_deadline <= created_at
           AND status NOT IN ('COMPLETED', 'CANCELLED')"""
    ).fetchall()

    total_cancelled = 0
    for row in rows:
        task_id = row["id"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM reminders WHERE task_id = ? AND status = 'PENDING'", (task_id,)
        ).fetchone()["c"]
        if pending:
            cancel_pending_reminders(conn, task_id=task_id)
            total_cancelled += pending
    return total_cancelled


def insert_reminder_if_absent(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    reminder_type: str,
    scheduled_for: str,
    idempotency_key: str,
) -> bool:
    """Returns True if a new reminder row was inserted, False if it already existed."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO reminders
           (task_id, reminder_type, scheduled_for, status, idempotency_key, created_at)
           VALUES (?, ?, ?, 'PENDING', ?, ?)""",
        (task_id, reminder_type, scheduled_for, idempotency_key, now_iso()),
    )
    conn.commit()
    return cur.rowcount > 0


def due_pending_reminders(conn: sqlite3.Connection, *, now: str) -> list[sqlite3.Row]:
    """PENDING reminders due to fire, including ones currently waiting out a
    bounded retry backoff (excluded until next_retry_at passes)."""
    return conn.execute(
        """SELECT reminders.*, tasks.title AS task_title, tasks.status AS task_status,
                  COALESCE(courses.course_code, courses.name) AS course_code
           FROM reminders
           JOIN tasks ON tasks.id = reminders.task_id
           JOIN courses ON courses.id = tasks.course_id
           WHERE reminders.status = 'PENDING' AND reminders.scheduled_for <= ?
           AND (reminders.next_retry_at IS NULL OR reminders.next_retry_at <= ?)
           AND tasks.status NOT IN ('COMPLETED', 'CANCELLED')
           ORDER BY reminders.scheduled_for ASC""",
        (now, now),
    ).fetchall()


def mark_reminder_for_retry(
    conn: sqlite3.Connection, *, reminder_id: int, error: str, attempt_count: int, next_retry_at: str
) -> None:
    """A send attempt failed but the bounded retry budget isn't exhausted
    yet - stays PENDING (still a legitimate candidate for dispatch), just
    not eligible again until next_retry_at."""
    conn.execute(
        "UPDATE reminders SET attempt_count = ?, last_error = ?, next_retry_at = ? WHERE id = ?",
        (attempt_count, error, next_retry_at, reminder_id),
    )
    conn.commit()


def mark_reminder_sent(conn: sqlite3.Connection, *, reminder_id: int) -> None:
    conn.execute(
        "UPDATE reminders SET status = 'SENT', sent_at = ? WHERE id = ?",
        (now_iso(), reminder_id),
    )
    conn.commit()


def mark_reminder_failed(
    conn: sqlite3.Connection, *, reminder_id: int, error: str, attempt_count: int | None = None
) -> None:
    """Terminal, permanent failure - retries exhausted (or none apply).
    attempt_count is optional so this can still be called directly (e.g. by
    tests or future callers) without a retry context, but the dispatch
    retry path always passes the final attempt count so it's not lost."""
    if attempt_count is None:
        conn.execute(
            "UPDATE reminders SET status = 'FAILED', last_error = ? WHERE id = ?",
            (error, reminder_id),
        )
    else:
        conn.execute(
            "UPDATE reminders SET status = 'FAILED', last_error = ?, attempt_count = ? WHERE id = ?",
            (error, attempt_count, reminder_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Calendar events
#
# One row per (task_id, kind) pair. The stored google_event_id is reused on
# every sync so events are updated in place, never duplicated - the calling
# sync layer is responsible for calling the Calendar API with this same id.
# ---------------------------------------------------------------------------


def get_calendar_event(conn: sqlite3.Connection, *, task_id: int, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM calendar_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()


def record_calendar_event(
    conn: sqlite3.Connection, *, task_id: int, kind: str, event_id: str
) -> None:
    """Insert or refresh the stored mapping for a Ragra-owned event.

    Idempotent: calling this repeatedly with the same (task_id, kind,
    event_id) never creates a second row.
    """
    now = now_iso()
    conn.execute(
        """INSERT INTO calendar_events (task_id, kind, google_event_id, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(google_event_id) DO UPDATE SET updated_at = excluded.updated_at""",
        (task_id, kind, event_id, now),
    )
    conn.commit()


def delete_calendar_event_record(conn: sqlite3.Connection, *, task_id: int, kind: str) -> None:
    conn.execute(
        "DELETE FROM calendar_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------


def record_sync_start(conn: sqlite3.Connection, *, source: str) -> None:
    now = now_iso()
    conn.execute(
        """INSERT INTO sync_state (source, last_synced_at, status)
           VALUES (?, ?, 'RUNNING')
           ON CONFLICT(source) DO UPDATE SET last_synced_at = excluded.last_synced_at, status = 'RUNNING'""",
        (source, now),
    )
    conn.commit()


def record_sync_success(conn: sqlite3.Connection, *, source: str) -> None:
    now = now_iso()
    conn.execute(
        """UPDATE sync_state SET last_success_at = ?, status = 'OK', last_error = NULL
           WHERE source = ?""",
        (now, source),
    )
    conn.commit()


def record_sync_error(conn: sqlite3.Connection, *, source: str, error: str) -> None:
    conn.execute(
        "UPDATE sync_state SET status = 'ERROR', last_error = ? WHERE source = ?",
        (error, source),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# FAST timetable events
# ---------------------------------------------------------------------------


@dataclass
class TimetableUpsertResult:
    event_id: int
    created: bool
    changed_fields: list[str] = field(default_factory=list)


_TIMETABLE_TRACKED_FIELDS = ("day_of_week", "start_time", "end_time", "room", "section", "status")


def get_timetable_event_by_external_id(conn: sqlite3.Connection, *, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM timetable_events WHERE external_id = ?", (external_id,)
    ).fetchone()


def upsert_timetable_event(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    course_name: str,
    program: str | None,
    batch_year: str | None,
    enrollment_type: str,
    day_of_week: int,
    occurrence_index: int,
    start_time: str,
    end_time: str,
    room: str | None,
    instructor: str | None,
    section: str | None,
    status: str,
    source_spreadsheet_id: str | None,
    source_sheet_gid: str | None,
    source_sheet_title: str | None,
) -> TimetableUpsertResult:
    """Idempotent by external_id (see schema.sql for how that's derived -
    never row/column position). Re-running a sync with unchanged data
    touches nothing; a genuine change (time, room, section, or a
    cancellation) updates the existing row in place rather than creating a
    new one, so a class moving from one day/room/time to another is a
    single UPDATE, not a delete-then-insert pair."""
    now = now_iso()
    existing = get_timetable_event_by_external_id(conn, external_id=external_id)

    if existing is None:
        cur = conn.execute(
            """INSERT INTO timetable_events
               (external_id, course_name, program, batch_year, enrollment_type, day_of_week,
                occurrence_index, start_time, end_time, room, instructor, section, status,
                source_spreadsheet_id, source_sheet_gid, source_sheet_title, last_synced_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                external_id, course_name, program, batch_year, enrollment_type, day_of_week,
                occurrence_index, start_time, end_time, room, instructor, section, status,
                source_spreadsheet_id, source_sheet_gid, source_sheet_title, now, now, now,
            ),
        )
        conn.commit()
        return TimetableUpsertResult(event_id=cur.lastrowid, created=True)

    new_values = {
        "day_of_week": day_of_week,
        "start_time": start_time,
        "end_time": end_time,
        "room": room,
        "section": section,
        "status": status,
    }
    changed = [f for f in _TIMETABLE_TRACKED_FIELDS if existing[f] != new_values[f]]

    conn.execute(
        """UPDATE timetable_events
           SET course_name = ?, program = ?, batch_year = ?, enrollment_type = ?, day_of_week = ?,
               occurrence_index = ?, start_time = ?, end_time = ?, room = ?, instructor = ?,
               section = ?, status = ?, source_spreadsheet_id = ?, source_sheet_gid = ?,
               source_sheet_title = ?, last_synced_at = ?, updated_at = ?
           WHERE id = ?""",
        (
            course_name, program, batch_year, enrollment_type, day_of_week, occurrence_index,
            start_time, end_time, room, instructor, section, status, source_spreadsheet_id,
            source_sheet_gid, source_sheet_title, now, now, existing["id"],
        ),
    )
    conn.commit()
    return TimetableUpsertResult(event_id=existing["id"], created=False, changed_fields=changed)


def cancel_timetable_events_missing_from_source(
    conn: sqlite3.Connection, *, seen_external_ids: set[str]
) -> list[int]:
    """A timetable sync that completed a structurally sound scrape (see
    ragra/sync/timetable_sync.py) but no longer sees a previously-known
    class marks it CANCELLED rather than deleting it - history is
    preserved, and a class temporarily missing due to a genuinely malformed
    scrape is never silently lost (callers must only call this after
    confirming the scrape was structurally sound)."""
    rows = conn.execute(
        "SELECT id, external_id FROM timetable_events WHERE status != 'CANCELLED'"
    ).fetchall()
    cancelled_ids = []
    now = now_iso()
    for row in rows:
        if row["external_id"] not in seen_external_ids:
            conn.execute(
                "UPDATE timetable_events SET status = 'CANCELLED', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            cancelled_ids.append(row["id"])
    if cancelled_ids:
        conn.commit()
    return cancelled_ids


def list_timetable_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM timetable_events ORDER BY day_of_week, start_time"
    ).fetchall()


# ---------------------------------------------------------------------------
# Tick session diagnostics (short-retention operational log, not app data)
# ---------------------------------------------------------------------------


def record_tick_session(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    exit_code: int,
    classroom_result: str | None,
    calendar_result: str | None,
    reminders_result: str | None,
    timetable_result: str | None,
    error: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO tick_sessions
           (started_at, finished_at, duration_seconds, exit_code, classroom_result,
            calendar_result, reminders_result, timetable_result, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            started_at, finished_at, duration_seconds, exit_code, classroom_result,
            calendar_result, reminders_result, timetable_result, error,
        ),
    )
    conn.commit()
    return cur.lastrowid


def purge_old_tick_sessions(conn: sqlite3.Connection, *, older_than_iso: str) -> int:
    """Deletes tick_sessions rows older than the given cutoff. Called at the
    start of every tick with a ~48-hour cutoff, so this operational log
    never grows without bound - it never touches any other table."""
    cur = conn.execute("DELETE FROM tick_sessions WHERE started_at < ?", (older_than_iso,))
    conn.commit()
    return cur.rowcount


def list_recent_tick_sessions(conn: sqlite3.Connection, *, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tick_sessions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
