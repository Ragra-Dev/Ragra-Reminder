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


# The pseudo-course every manually created task belongs to (migration 0007).
PERSONAL_COURSE_EXTERNAL_ID = "__personal__"
MANUAL_SOURCE_TYPE = "manual"


class TaskSourceViolation(RuntimeError):
    """An attempt to perform a manual-task operation on a Classroom-sourced
    task. Raised, never silently ignored: Classroom is authoritative for the
    tasks it owns, and quietly discarding such a write would leave the user
    believing an edit had been applied.

    See docs/INTERFACES.md contract #5 for exactly which fields are
    Ragra-owned (personal deadline, completion - editable on any task) and
    which are Classroom-authoritative (never user-writable)."""


def _require_manual_task(conn: sqlite3.Connection, *, task_id: int, operation: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, source_type FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskSourceViolation(f"cannot {operation}: task {task_id} does not exist")
    if row["source_type"] != MANUAL_SOURCE_TYPE:
        raise TaskSourceViolation(
            f"cannot {operation}: task {task_id} is Classroom-sourced "
            f"({row['source_type']}) and is not editable"
        )
    return row


# ---------------------------------------------------------------------------
# Users (tenant anchor - migration 0009)
# ---------------------------------------------------------------------------


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users ORDER BY id").fetchall()


def get_user(conn: sqlite3.Connection, *, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_google_sub(conn: sqlite3.Connection, *, google_sub: str) -> sqlite3.Row | None:
    """Look a user up by Google's stable subject id - the only identity key.
    Never look a user up by email: a Google account's email can change while
    its subject id cannot, so email matching would let one account silently
    inherit another's data."""
    return conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()


def unlinked_user_id(conn: sqlite3.Connection) -> int | None:
    """The id of the single pre-identity owner row (google_sub IS NULL), or
    None if there isn't exactly one.

    This exists for one narrow purpose: the database predates any concept of
    identity, so all of its existing data belongs to an owner who has never
    signed in. Sign-in (P3-5) adopts that row rather than creating a second
    user, which is what stops the entire existing history from becoming
    orphaned the moment authentication is introduced.

    Deliberately returns None when more than one unlinked row exists - an
    ambiguous adoption must fail loudly rather than pick one."""
    rows = conn.execute("SELECT id FROM users WHERE google_sub IS NULL").fetchall()
    return rows[0]["id"] if len(rows) == 1 else None



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


def update_course_state_if_known(conn: sqlite3.Connection, *, external_id: str, state: str) -> None:
    """Keep a previously-synced course's stored state accurate even after
    Ragra stops actively syncing it (e.g. it goes ARCHIVED at the source and
    the sync loop stops discovering new items for it). Update-only - never
    creates a course row, since a course Ragra has never held data for is
    simply irrelevant. This is what lets due_pending_reminders exclude tasks
    tied to a since-archived course without deleting any of their history."""
    conn.execute(
        "UPDATE courses SET state = ?, updated_at = ? WHERE external_id = ?",
        (state, now_iso(), external_id),
    )
    conn.commit()


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


def personal_course_id(conn: sqlite3.Connection) -> int:
    """The pseudo-course every manual task belongs to (migration 0007)."""
    row = conn.execute(
        "SELECT id FROM courses WHERE external_id = ?", (PERSONAL_COURSE_EXTERNAL_ID,)
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "the '__personal__' pseudo-course is missing; migration 0007 has not been applied"
        )
    return row["id"]


def create_manual_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str | None = None,
    actual_deadline: str | None = None,
    personal_deadline: str | None = None,
) -> int:
    """Create a task the user owns outright.

    `actual_deadline` carries the user's own stated due time. That is not a
    contradiction of docs/DOMAIN.md - actual_deadline means "the
    authoritative deadline for this task", and for a manual task the user
    *is* the authority, there being no external source. Keeping it in the
    same column is what lets manual tasks reuse reminder scheduling,
    overdue/missed transitions and every deadline query unchanged, instead
    of growing a parallel set of manual-only code paths. personal_deadline
    keeps its usual separate meaning: when the user intends to do the work.

    source_type and external_id are derived here, never accepted as
    parameters (docs/INTERFACES.md contract #5).
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("a manual task needs a title")

    now = now_iso()
    initial_status = "PLANNED" if personal_deadline else "ACTION_REQUIRED"
    cur = conn.execute(
        """INSERT INTO tasks
           (course_id, source_type, external_id, title, description, link, kind, status,
            actual_deadline, personal_deadline, source_published_at, source_updated_at,
            created_at, updated_at)
           VALUES (?, ?, NULL, ?, ?, NULL, 'ACTIONABLE', ?, ?, ?, NULL, NULL, ?, ?)""",
        (
            personal_course_id(conn), MANUAL_SOURCE_TYPE, clean_title, description,
            initial_status, actual_deadline, personal_deadline, now, now,
        ),
    )
    task_id = cur.lastrowid
    record_history(conn, task_id=task_id, field_name="created", old_value=None, new_value=clean_title)
    conn.commit()
    return task_id


def update_manual_task(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    actual_deadline: str | None = None,
) -> None:
    """Edit a manual task's own fields. Raises TaskSourceViolation for any
    Classroom-sourced task - those fields belong to Classroom and are
    rewritten by the next sync regardless, so allowing an edit would be
    both a boundary violation and a lie to the user.

    Only the arguments actually supplied are changed; passing nothing is a
    no-op rather than a silent wipe of every field."""
    _require_manual_task(conn, task_id=task_id, operation="edit task")

    updates: dict[str, str | None] = {}
    if title is not None:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("a manual task needs a title")
        updates["title"] = clean_title
    if description is not None:
        updates["description"] = description
    if actual_deadline is not None:
        updates["actual_deadline"] = actual_deadline
    if not updates:
        return

    existing = conn.execute(
        "SELECT title, description, actual_deadline FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()

    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), now_iso(), task_id),
    )
    for column, new_value in updates.items():
        if existing[column] != new_value:
            record_history(
                conn, task_id=task_id, field_name=column,
                old_value=existing[column], new_value=new_value,
            )
    conn.commit()


def manual_tasks(conn: sqlite3.Connection, *, include_finished: bool = False) -> list[sqlite3.Row]:
    clause = "" if include_finished else "AND tasks.status NOT IN ('COMPLETED', 'CANCELLED')"
    return conn.execute(
        f"""SELECT tasks.*, courses.course_code, courses.name AS course_name
            FROM tasks JOIN courses ON courses.id = tasks.course_id
            WHERE tasks.source_type = '{MANUAL_SOURCE_TYPE}' {clause}
            ORDER BY COALESCE(tasks.actual_deadline, tasks.personal_deadline) IS NULL,
                     COALESCE(tasks.actual_deadline, tasks.personal_deadline) ASC"""
    ).fetchall()


def open_announcements(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    """Announcements still awaiting triage, newest first. Archived ones drop
    out; already-actioned ones stay but carry their child task's id so the
    UI can show that they were handled."""
    sql = """SELECT tasks.*, courses.course_code, courses.name AS course_name,
                    (SELECT child.id FROM tasks child
                      WHERE child.parent_task_id = tasks.id
                        AND child.status NOT IN ('CANCELLED')
                      LIMIT 1) AS child_task_id
             FROM tasks JOIN courses ON courses.id = tasks.course_id
             WHERE tasks.source_type = 'announcement'
               AND tasks.status NOT IN ('ARCHIVED', 'CANCELLED')
             ORDER BY COALESCE(tasks.source_published_at, tasks.created_at) DESC"""
    if limit is not None:
        return conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def archive_task(conn: sqlite3.Connection, *, task_id: int) -> None:
    """Mark a task as triaged-and-done-with. Allowed on any task, including
    Classroom-sourced ones: like completion, this is a Ragra-owned fact
    about the user's own handling of the item, not a claim about whether it
    still exists at the source. Distinct from cancellation, which asserts
    the task should not exist at all and is therefore restricted to manual
    tasks (docs/INTERFACES.md contract #5)."""
    now = now_iso()
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] == "ARCHIVED":
        return
    conn.execute(
        "UPDATE tasks SET status = 'ARCHIVED', updated_at = ? WHERE id = ?", (now, task_id)
    )
    record_history(
        conn, task_id=task_id, field_name="status", old_value=row["status"], new_value="ARCHIVED"
    )
    conn.commit()


def create_task_from_announcement(
    conn: sqlite3.Connection,
    *,
    announcement_task_id: int,
    title: str | None = None,
    actual_deadline: str | None = None,
    personal_deadline: str | None = None,
) -> int:
    """Turn an announcement into a personal task the user owns.

    Idempotent by the parent link, not by title: submitting the form twice
    (or a browser retrying a POST) returns the existing child rather than
    creating a duplicate. The announcement itself is never modified - it
    stays exactly as Classroom published it.
    """
    announcement = conn.execute(
        "SELECT id, title, source_type FROM tasks WHERE id = ?", (announcement_task_id,)
    ).fetchone()
    if announcement is None:
        raise ValueError(f"announcement task {announcement_task_id} does not exist")
    if announcement["source_type"] != "announcement":
        raise ValueError(
            f"task {announcement_task_id} is not an announcement "
            f"(source_type={announcement['source_type']})"
        )

    existing = conn.execute(
        """SELECT id FROM tasks
           WHERE parent_task_id = ? AND status NOT IN ('CANCELLED') LIMIT 1""",
        (announcement_task_id,),
    ).fetchone()
    if existing is not None:
        return existing["id"]

    task_id = create_manual_task(
        conn,
        title=title or announcement["title"],
        description=None,
        actual_deadline=actual_deadline,
        personal_deadline=personal_deadline,
    )
    conn.execute("UPDATE tasks SET parent_task_id = ? WHERE id = ?", (announcement_task_id, task_id))
    conn.commit()
    return task_id


def set_task_relevance(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    relevance: str,
    reason: str | None,
    computed_at: str | None = None,
) -> bool:
    """Persist the section-relevance decision for a task. Returns True if the
    stored decision actually changed.

    Writing is skipped entirely when the decision is unchanged, so a routine
    re-sync neither churns updated_at nor floods task_history - the decision
    is derived from title/description/profile, all of which are stable
    between polls.

    This never changes task visibility: relevance is advisory metadata that
    the notification path consults. A task is always stored, always listed,
    and always visible regardless of the value written here.
    """
    row = conn.execute("SELECT relevance, relevance_reason FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return False
    if row["relevance"] == relevance and row["relevance_reason"] == reason:
        return False

    conn.execute(
        "UPDATE tasks SET relevance = ?, relevance_reason = ?, relevance_computed_at = ? WHERE id = ?",
        (relevance, reason, computed_at or now_iso(), task_id),
    )
    record_history(
        conn, task_id=task_id, field_name="relevance",
        old_value=row["relevance"], new_value=relevance,
    )
    conn.commit()
    return True


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
    append duplicate history rows for a task already marked missed.

    Also cancels the task's own PENDING reminders at the same call site -
    the same pairing already used for completion (see web/app.py) and
    source-side cancellation (see cancel_tasks_missing_from_source) - so a
    task that only just became overdue this tick can never fire a
    now-nonsensical "due soon"/"due in 1 hour" reminder after the fact."""
    rows = conn.execute(
        """SELECT id FROM tasks
           WHERE actual_deadline IS NOT NULL AND actual_deadline < ?
           AND status NOT IN ('COMPLETED', 'CANCELLED', 'MISSED')""",
        (now,),
    ).fetchall()
    missed_task_ids = []
    for row in rows:
        mark_missed(conn, task_id=row["id"])
        cancel_pending_reminders(conn, task_id=row["id"])
        missed_task_ids.append(row["id"])
    return missed_task_ids


def cancel_task_from_source(conn: sqlite3.Connection, *, task_id: int) -> None:
    """System-initiated cancellation, used when Classroom itself stops
    returning an item. Deliberately unguarded: the source is allowed to
    cancel its own tasks. Never call this from a user-facing route - use
    cancel_task, which enforces the boundary."""
    now = now_iso()
    conn.execute(
        "UPDATE tasks SET status = 'CANCELLED', cancelled_at = ?, updated_at = ? WHERE id = ?",
        (now, now, task_id),
    )
    record_history(conn, task_id=task_id, field_name="status", old_value=None, new_value="CANCELLED")
    conn.commit()


def cancel_task(conn: sqlite3.Connection, *, task_id: int) -> None:
    """User-initiated cancellation. Raises TaskSourceViolation for a
    Classroom-sourced task: whether such a task exists is Classroom's
    decision, not the user's (docs/INTERFACES.md contract #5). Completing it
    is always allowed - that is a Ragra-owned fact about the user's own
    progress."""
    _require_manual_task(conn, task_id=task_id, operation="cancel task")
    cancel_task_from_source(conn, task_id=task_id)


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
            cancel_task_from_source(conn, task_id=row["id"])
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
    surface these so the user can decide when they plan to actually do the
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


def cancel_stray_reminders_for_terminal_tasks(conn: sqlite3.Connection) -> int:
    """Safety net, not a one-off migration (same style as
    cancel_backlog_reminders_for_already_overdue_tasks above): a task that is
    COMPLETED, CANCELLED, or MISSED must never carry a PENDING reminder - the
    normal state-transition call sites are supposed to cancel it already
    (see mark_overdue_tasks_as_missed, cancel_task, and web/app.py's complete
    handler), but this self-heals anything written before that pairing
    existed at a given call site, or by any path this doesn't yet cover.
    Idempotent and safe to run on every sync. Only cancels PENDING rows -
    SENT history is never touched or deleted. Returns the number of reminder
    rows actually cancelled."""
    rows = conn.execute(
        """SELECT reminders.id FROM reminders
           JOIN tasks ON tasks.id = reminders.task_id
           WHERE reminders.status = 'PENDING'
           AND tasks.status IN ('COMPLETED', 'CANCELLED', 'MISSED')"""
    ).fetchall()
    if not rows:
        return 0
    conn.executemany(
        "UPDATE reminders SET status = 'CANCELLED' WHERE id = ?",
        [(row["id"],) for row in rows],
    )
    conn.commit()
    return len(rows)


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
    bounded retry backoff (excluded until next_retry_at passes).

    Two eligibility guards, both defense-in-depth on top of the state
    transitions that are supposed to cancel a reminder proactively: a task
    already MISSED must never fire a "due soon" reminder for a deadline
    that's already passed (mirrors the existing COMPLETED/CANCELLED
    exclusion), and a task whose course is no longer ACTIVE/PROVISIONED
    (archived at the source) must never generate a normal reminder either -
    only currently active/enrolled courses are eligible."""
    return conn.execute(
        """SELECT reminders.*, tasks.title AS task_title, tasks.status AS task_status,
                  COALESCE(courses.course_code, courses.name) AS course_code
           FROM reminders
           JOIN tasks ON tasks.id = reminders.task_id
           JOIN courses ON courses.id = tasks.course_id
           WHERE reminders.status = 'PENDING' AND reminders.scheduled_for <= ?
           AND (reminders.next_retry_at IS NULL OR reminders.next_retry_at <= ?)
           AND tasks.status NOT IN ('COMPLETED', 'CANCELLED', 'MISSED')
           AND courses.state IN ('ACTIVE', 'PROVISIONED')
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


# ---------------------------------------------------------------------------
# Notification delivery status
# ---------------------------------------------------------------------------


def record_notification_delivery(
    conn: sqlite3.Connection,
    *,
    provider: str,
    ok: bool,
    reminder_id: int | None = None,
    category: str | None = None,
    error: str | None = None,
) -> None:
    """Record one provider's attempt. `provider` is the provider's class
    name only - never its configuration, which would put credentials into a
    table the dashboard renders."""
    conn.execute(
        """INSERT INTO notification_deliveries
           (reminder_id, category, provider, ok, error, attempted_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (reminder_id, category, provider, 1 if ok else 0, error, now_iso()),
    )
    conn.commit()


def recent_notification_deliveries(conn: sqlite3.Connection, *, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notification_deliveries ORDER BY attempted_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def deliveries_for_reminder(conn: sqlite3.Connection, *, reminder_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notification_deliveries WHERE reminder_id = ? ORDER BY id",
        (reminder_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Class reminders (see ragra/reminders/class_reminders.py)
# ---------------------------------------------------------------------------


def insert_class_reminder_if_absent(
    conn: sqlite3.Connection,
    *,
    timetable_event_id: int,
    occurrence_date: str,
    reminder_type: str,
    scheduled_for: str,
) -> int | None:
    """Claim one class occurrence for notification. Returns the new row id,
    or None if this occurrence was already claimed.

    The UNIQUE idempotency_key is what makes this safe to call on every
    tick: a second call for the same occurrence inserts nothing, so a class
    can never be announced twice no matter how often the tick runs."""
    key = f"{timetable_event_id}:{occurrence_date}:{reminder_type}"
    cur = conn.execute(
        """INSERT OR IGNORE INTO class_reminders
           (timetable_event_id, occurrence_date, reminder_type, scheduled_for,
            status, idempotency_key, created_at)
           VALUES (?, ?, ?, ?, 'PENDING', ?, ?)""",
        (timetable_event_id, occurrence_date, reminder_type, scheduled_for, key, now_iso()),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def pending_class_reminders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT class_reminders.*, timetable_events.course_name, timetable_events.room,
                  timetable_events.section, timetable_events.status AS class_status
           FROM class_reminders
           JOIN timetable_events ON timetable_events.id = class_reminders.timetable_event_id
           WHERE class_reminders.status = 'PENDING'
           ORDER BY class_reminders.scheduled_for ASC"""
    ).fetchall()


def mark_class_reminder_sent(conn: sqlite3.Connection, *, class_reminder_id: int) -> None:
    now = now_iso()
    conn.execute(
        "UPDATE class_reminders SET status = 'SENT', sent_at = ?, attempt_count = attempt_count + 1 WHERE id = ?",
        (now, class_reminder_id),
    )
    conn.commit()


def record_class_reminder_attempt(
    conn: sqlite3.Connection, *, class_reminder_id: int, error: str, give_up: bool
) -> None:
    """A failed send. Stays PENDING (and is retried on the next tick) while
    the class has not started yet; becomes terminal FAILED once it has,
    because a class reminder delivered after the class began is worse than
    none at all."""
    conn.execute(
        """UPDATE class_reminders
           SET status = CASE WHEN ? THEN 'FAILED' ELSE 'PENDING' END,
               last_error = ?, attempt_count = attempt_count + 1
           WHERE id = ?""",
        (1 if give_up else 0, error, class_reminder_id),
    )
    conn.commit()


def expire_stale_class_reminders(conn: sqlite3.Connection, *, now: str) -> int:
    """Any PENDING class reminder whose class has already started is stale -
    mark it FAILED rather than letting it fire late."""
    cur = conn.execute(
        """UPDATE class_reminders
           SET status = 'FAILED', last_error = 'class already started; reminder expired'
           WHERE status = 'PENDING' AND scheduled_for <= ?""",
        (now,),
    )
    conn.commit()
    return cur.rowcount


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
    class_reminders_result: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO tick_sessions
           (started_at, finished_at, duration_seconds, exit_code, classroom_result,
            calendar_result, reminders_result, timetable_result, class_reminders_result, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            started_at, finished_at, duration_seconds, exit_code, classroom_result,
            calendar_result, reminders_result, timetable_result, class_reminders_result, error,
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
