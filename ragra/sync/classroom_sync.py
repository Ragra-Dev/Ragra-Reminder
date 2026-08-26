"""Classroom -> Ragra database sync.

Takes an already-authenticated classroom client (see
ragra/adapters/classroom.py) and reconciles it into the local database.
Idempotent: safe to run every few minutes. Uses stable Classroom ids
(course id + coursework/announcement/materiel id) for all deduplication,
never titles, per the classroom sync rules.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from ragra.db import repo
from ragra.reminders.engine import compute_reminder_plan
from datetime import datetime, timezone


class ClassroomClient(Protocol):
    def list_courses(self) -> list[dict[str, Any]]: ...
    def list_course_work(self, course_id: str) -> list[dict[str, Any]]: ...
    def list_announcements(self, course_id: str) -> list[dict[str, Any]]: ...
    def list_course_materials(self, course_id: str) -> list[dict[str, Any]]: ...


@dataclass
class SyncSummary:
    courses_seen: int = 0
    tasks_created: int = 0
    tasks_updated: int = 0
    tasks_cancelled: int = 0
    backlog_reminders_suppressed: int = 0
    tasks_marked_missed: int = 0
    deadlines_changed: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _classroom_due_to_iso(due_date: dict | None, due_time: dict | None) -> str | None:
    """Classroom returns due date/time as separate structured objects. Both
    absent means no due date - never invent one."""
    if not due_date:
        return None
    hour = (due_time or {}).get("hours", 23)
    minute = (due_time or {}).get("minutes", 59)
    try:
        dt = datetime(
            due_date["year"], due_date["month"], due_date["day"],
            hour, minute, tzinfo=timezone.utc,
        )
    except (KeyError, ValueError):
        return None
    return dt.isoformat()


def sync_classroom(conn: sqlite3.Connection, client: ClassroomClient) -> SyncSummary:
    summary = SyncSummary()
    repo.record_sync_start(conn, source="classroom")

    try:
        courses = client.list_courses()
        for course in courses:
            if course.get("courseState") not in ("ACTIVE", "PROVISIONED"):
                continue
            summary.courses_seen += 1
            course_id = repo.upsert_course(
                conn,
                external_id=course["id"],
                name=course.get("name", "Untitled course"),
                section=course.get("section"),
                # No teacher-name lookup: courses.teachers.list needs a
                # roster scope Ragra deliberately never requests (least
                # privilege), so it would fail on every course, every sync,
                # forever - not a transient condition worth retrying. No
                # current feature reads this column; it stays None by
                # design rather than spending an API call per course per
                # tick on a lookup that can never succeed under this policy.
                teacher=None,
                # The Classroom API has no short "course code" field (no
                # CS1004-style identifier) - courseGroupEmail is a mailing
                # address, not a code, and must not be used as one.
                # Matching against a known course-registration table (e.g.
                # Hermes' matching.py) is a separate, future enhancement;
                # for now every display path falls back to the real course
                # name (see due_pending_reminders' COALESCE and the
                # dashboard template's `course_code or course_name`).
                course_code=None,
                state=course.get("courseState", "ACTIVE"),
            )

            seen_coursework = _sync_coursework(conn, client, course_id, course["id"], summary)
            seen_announcements = _sync_announcements(conn, client, course_id, course["id"], summary)
            seen_materials = _sync_materials(conn, client, course_id, course["id"], summary)

            for source_type, seen_ids in (
                ("coursework", seen_coursework),
                ("announcement", seen_announcements),
                ("material", seen_materials),
            ):
                cancelled = repo.cancel_tasks_missing_from_source(
                    conn, course_id=course_id, source_type=source_type, seen_external_ids=seen_ids
                )
                summary.tasks_cancelled += len(cancelled)

        summary.backlog_reminders_suppressed = repo.cancel_backlog_reminders_for_already_overdue_tasks(conn)
        summary.tasks_marked_missed = len(repo.mark_overdue_tasks_as_missed(conn, now=repo.now_iso()))

        repo.record_sync_success(conn, source="classroom")
    except Exception as exc:  # noqa: BLE001 - sync must never crash the process
        summary.errors.append(str(exc))
        repo.record_sync_error(conn, source="classroom", error=str(exc))

    return summary


def _apply_upsert(
    conn: sqlite3.Connection,
    *,
    course_id: int,
    course_external_id: str,
    course_code: str | None,
    source_type: str,
    item: dict[str, Any],
    kind: str,
    actual_deadline: str | None,
    summary: SyncSummary,
) -> None:
    result = repo.upsert_task_from_source(
        conn,
        course_id=course_id,
        source_type=source_type,
        external_id=item["id"],
        title=item.get("title") or item.get("text", "")[:120] or "(untitled)",
        description=item.get("description") or item.get("text"),
        link=item.get("alternateLink"),
        kind=kind,
        actual_deadline=actual_deadline,
        source_published_at=item.get("creationTime"),
        source_updated_at=item.get("updateTime"),
    )

    if result.created:
        summary.tasks_created += 1
        if actual_deadline:
            # discovered_at is intentionally Ragra's OWN "right now", not
            # Classroom's item creationTime. Anchoring the reminder cadence
            # to when the professor originally posted the item (which can
            # be months in the past for a first-ever historical import)
            # produces a flood of reminder windows that already elapsed
            # before Ragra ever existed to fire them. Anchoring to when
            # Ragra itself first learned about the task is what actually
            # distinguishes "historical backlog" from "genuinely new work":
            # a task already past its deadline at the moment Ragra
            # discovers it correctly gets zero pre-deadline reminders
            # (compute_reminder_plan already returns [] for a non-positive
            # lead time), while a task discovered with real time left
            # before its deadline gets the normal countdown.
            _schedule_reminders(conn, task_id=result.task_id, title=item.get("title") or "",
                                 course_code=course_code, actual_deadline=actual_deadline,
                                 discovered_at=None)
        return

    if result.deadline_changed or result.other_fields_changed:
        summary.tasks_updated += 1

    if result.deadline_changed:
        summary.deadlines_changed.append(
            {
                "task_id": result.task_id,
                "title": item.get("title"),
                "old_deadline": result.old_deadline,
                "new_deadline": result.new_deadline,
            }
        )
        repo.cancel_pending_reminders(conn, task_id=result.task_id)
        if result.new_deadline:
            _schedule_reminders(conn, task_id=result.task_id, title=item.get("title") or "",
                                 course_code=course_code, actual_deadline=result.new_deadline,
                                 discovered_at=repo.now_iso())


def _schedule_reminders(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    title: str,
    course_code: str | None,
    actual_deadline: str,
    discovered_at: str | None,
) -> None:
    deadline_dt = datetime.fromisoformat(actual_deadline)
    discovered_dt = datetime.fromisoformat(discovered_at) if discovered_at else datetime.now(timezone.utc)
    if discovered_dt.tzinfo is None:
        discovered_dt = discovered_dt.replace(tzinfo=timezone.utc)

    for plan_item in compute_reminder_plan(actual_deadline=deadline_dt, discovered_at=discovered_dt):
        scheduled_iso = plan_item.scheduled_for.isoformat()
        idempotency_key = f"{task_id}:{plan_item.reminder_type}:{actual_deadline}"
        repo.insert_reminder_if_absent(
            conn,
            task_id=task_id,
            reminder_type=plan_item.reminder_type,
            scheduled_for=scheduled_iso,
            idempotency_key=idempotency_key,
        )


def _sync_coursework(conn, client, course_id, course_external_id, summary) -> set[str]:
    course_code = None
    items = client.list_course_work(course_external_id)
    for item in items:
        deadline = _classroom_due_to_iso(item.get("dueDate"), item.get("dueTime"))
        _apply_upsert(
            conn, course_id=course_id, course_external_id=course_external_id,
            course_code=course_code, source_type="coursework", item=item,
            kind="ACTIONABLE", actual_deadline=deadline, summary=summary,
        )
    return {item["id"] for item in items}


def _sync_announcements(conn, client, course_id, course_external_id, summary) -> set[str]:
    items = client.list_announcements(course_external_id)
    for item in items:
        _apply_upsert(
            conn, course_id=course_id, course_external_id=course_external_id,
            course_code=None, source_type="announcement", item=item,
            kind="INFORMATIONAL", actual_deadline=None, summary=summary,
        )
    return {item["id"] for item in items}


def _sync_materials(conn, client, course_id, course_external_id, summary) -> set[str]:
    items = client.list_course_materials(course_external_id)
    for item in items:
        _apply_upsert(
            conn, course_id=course_id, course_external_id=course_external_id,
            course_code=None, source_type="material", item=item,
            kind="INFORMATIONAL", actual_deadline=None, summary=summary,
        )
    return {item["id"] for item in items}
