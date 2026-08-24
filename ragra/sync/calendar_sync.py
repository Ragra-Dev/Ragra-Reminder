"""Task -> Google Calendar sync.

Idempotent: reuses the stored event id for a (task, kind) pair so repeated
runs update the existing Calendar event rather than creating a duplicate.
Completed or cancelled tasks that already have an event get that event
removed. Missed tasks keep their event as a historical marker of when the
work was due.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ragra.adapters.calendar import CalendarClient
from ragra.db import repo

EVENT_KIND_ACTUAL_DEADLINE = "ACTUAL_DEADLINE"


def _shift_minutes(iso_dt: str, minutes: int) -> str:
    dt = datetime.fromisoformat(iso_dt)
    return (dt + timedelta(minutes=minutes)).isoformat()


def _event_body(task: sqlite3.Row) -> dict[str, Any]:
    course_label = task["course_code"] or task["course_name"]
    summary = f"{course_label}: {task['title']}" if course_label else task["title"]
    deadline = task["actual_deadline"]
    return {
        "summary": summary,
        "description": f"Ragra academic deadline. Source: {task['source_type']} {task['external_id']}",
        "start": {"dateTime": _shift_minutes(deadline, -15)},
        "end": {"dateTime": deadline},
    }


def sync_task_event(
    conn: sqlite3.Connection,
    client: CalendarClient,
    *,
    calendar_id: str,
    task_id: int,
    kind: str = EVENT_KIND_ACTUAL_DEADLINE,
) -> str:
    """Reconcile a single task's calendar event.

    Returns 'created', 'updated', 'removed', or 'skipped'.
    """
    task = conn.execute(
        """SELECT tasks.*, courses.course_code, courses.name AS course_name
           FROM tasks JOIN courses ON courses.id = tasks.course_id
           WHERE tasks.id = ?""",
        (task_id,),
    ).fetchone()
    if task is None:
        return "skipped"

    existing = repo.get_calendar_event(conn, task_id=task_id, kind=kind)

    should_have_event = (
        task["actual_deadline"] is not None
        and task["status"] not in ("CANCELLED", "COMPLETED")
    )

    if not should_have_event:
        if existing is not None:
            client.delete_event(calendar_id, existing["google_event_id"])
            repo.delete_calendar_event_record(conn, task_id=task_id, kind=kind)
            return "removed"
        return "skipped"

    body = _event_body(task)

    if existing is None:
        event = client.create_event(calendar_id, body)
        repo.record_calendar_event(conn, task_id=task_id, kind=kind, event_id=event["id"])
        return "created"

    client.update_event(calendar_id, existing["google_event_id"], body)
    repo.record_calendar_event(conn, task_id=task_id, kind=kind, event_id=existing["google_event_id"])
    return "updated"


def sync_all_task_events(
    conn: sqlite3.Connection, client: CalendarClient, *, calendar_id: str
) -> dict[str, int]:
    """Reconcile every task that either has a deadline or still has a
    stored Ragra-owned event (so removal happens even if the deadline was
    since cleared). Safe to call every sync cycle - see sync_task_event."""
    task_ids = {
        row["id"]
        for row in conn.execute("SELECT id FROM tasks WHERE actual_deadline IS NOT NULL").fetchall()
    }
    task_ids |= {
        row["task_id"] for row in conn.execute("SELECT DISTINCT task_id FROM calendar_events").fetchall()
    }

    counts = {"created": 0, "updated": 0, "removed": 0, "skipped": 0}
    for task_id in sorted(task_ids):
        outcome = sync_task_event(conn, client, calendar_id=calendar_id, task_id=task_id)
        counts[outcome] += 1
    return counts
