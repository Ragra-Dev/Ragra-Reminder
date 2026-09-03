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
from ragra.relevance.engine import RelevanceDecision, is_relevant
from ragra.relevance.profile import UserAcademicProfile, load_profile
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
    relevance_evaluated: int = 0
    relevance_other_section: int = 0
    deadlines_changed: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Non-fatal problems. A relevance failure lands here rather than in
    # `errors`, because it must never mark the whole Classroom sync failed:
    # the academic data synced correctly, only the advisory decision didn't.
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _RelevanceContext:
    """Everything the relevance engine needs for one course, resolved once
    per sync rather than per item. `profile` is loaded a single time for the
    whole run (see load_profile's contract in docs/INTERFACES.md #4)."""

    course_name: str
    profile: UserAcademicProfile


def _evaluate_relevance(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    task_id: int,
    item: dict[str, Any],
    context: _RelevanceContext | None,
    summary: SyncSummary,
) -> None:
    """Compute and persist the section-relevance decision for one task.

    Deliberately best-effort and fail-open in three separate ways: no
    context means nothing is written (every task keeps the RELEVANT default
    from migration 0002); a raised exception is recorded as a warning and
    swallowed rather than failing the sync; and the decision itself never
    affects whether the task is stored or listed. Relevance can only ever
    influence proactive notification - never visibility.
    """
    if context is None:
        return
    try:
        decision = is_relevant(
            item.get("title") or item.get("text", "") or "",
            item.get("description") or item.get("text") or "",
            context.course_name,
            context.profile,
        )
    except Exception as exc:  # noqa: BLE001 - relevance must never break sync
        summary.warnings.append(f"relevance evaluation failed for task {task_id}: {exc}")
        return

    reason = None
    if decision is not RelevanceDecision.RELEVANT:
        # A richer trace would need the engine to return its evidence;
        # docs/INTERFACES.md #3 freezes the return type as the enum, so the
        # reason records what was decided and against which course.
        reason = f"{decision.value} for course {context.course_name!r}"

    summary.relevance_evaluated += 1
    if decision is RelevanceDecision.OTHER_SECTION:
        summary.relevance_other_section += 1
    repo.set_task_relevance(
        conn, user_id=user_id, task_id=task_id, relevance=decision.value, reason=reason
    )


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


def sync_classroom(
    conn: sqlite3.Connection, client: ClassroomClient, *, user_id: int
) -> SyncSummary:
    """Reconcile one user's Classroom account. `client` is authenticated as
    that user, so every write here is scoped to the same user_id - the sync
    never reads or writes another account's rows even when two users are
    enrolled in the same Classroom course."""
    summary = SyncSummary()
    repo.record_sync_start(conn, user_id=user_id, source="classroom")

    # Loaded once for the whole run. A failure here disables relevance
    # evaluation for this sync but must not stop academic data syncing -
    # every task then keeps the fail-open RELEVANT default.
    profile: UserAcademicProfile | None
    try:
        profile = load_profile(conn, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - advisory feature, never fatal
        profile = None
        summary.warnings.append(f"relevance disabled this run: profile unavailable ({exc})")

    try:
        courses = client.list_courses()
        for course in courses:
            course_state = course.get("courseState", "ACTIVE")
            # Deliberately an allow-list (only ACTIVE/PROVISIONED proceed),
            # not a deny-list of known-bad states - so it correctly excludes
            # every other state Classroom's Course.courseState enum actually
            # defines (ARCHIVED, DECLINED, SUSPENDED, COURSE_STATE_UNSPECIFIED)
            # without needing to enumerate each one, and stays correct if
            # Google ever adds a new state. Investigated directly against the
            # official Classroom API v1 discovery document: there is no
            # separate "hidden" course state exposed anywhere in the API -
            # the student-facing "hide this class" toggle in Classroom's own
            # UI is a client-side-only preference Google does not return
            # through the API at all, so there is nothing for Ragra to read
            # or filter on for it specifically. If Google ever does expose
            # such a state as a courseState value, this same allow-list
            # excludes it automatically, with zero code change needed.
            if course_state not in ("ACTIVE", "PROVISIONED"):
                # Keep a previously-known course's stored state accurate even
                # though Ragra stops discovering new items for it - this is
                # what lets due_pending_reminders exclude tasks tied to a
                # since-archived course, without deleting any history and
                # without ever creating a row for a course never seen before.
                repo.update_course_state_if_known(
                    conn, user_id=user_id, external_id=course["id"], state=course_state
                )
                continue
            summary.courses_seen += 1
            course_id = repo.upsert_course(
                conn,
                user_id=user_id,
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
                state=course_state,
            )

            course_name = course.get("name", "Untitled course")
            context = _RelevanceContext(course_name=course_name, profile=profile) if profile else None

            seen_coursework = _sync_coursework(
                conn, client, user_id, course_id, course["id"], summary, context
            )
            seen_announcements = _sync_announcements(
                conn, client, user_id, course_id, course["id"], summary, context
            )
            seen_materials = _sync_materials(
                conn, client, user_id, course_id, course["id"], summary, context
            )

            for source_type, seen_ids in (
                ("coursework", seen_coursework),
                ("announcement", seen_announcements),
                ("material", seen_materials),
            ):
                cancelled = repo.cancel_tasks_missing_from_source(
                    conn, user_id=user_id, course_id=course_id, source_type=source_type,
                    seen_external_ids=seen_ids,
                )
                summary.tasks_cancelled += len(cancelled)

        summary.backlog_reminders_suppressed = (
            repo.cancel_backlog_reminders_for_already_overdue_tasks(conn, user_id=user_id)
        )
        summary.tasks_marked_missed = len(
            repo.mark_overdue_tasks_as_missed(conn, user_id=user_id, now=repo.now_iso())
        )
        # Self-healing safety net (idempotent, safe every sync): cleans up
        # any PENDING reminder left behind on an already-terminal task by
        # data written before a given state-transition call site paired
        # itself with cancel_pending_reminders.
        repo.cancel_stray_reminders_for_terminal_tasks(conn, user_id=user_id)

        repo.record_sync_success(conn, user_id=user_id, source="classroom")
    except Exception as exc:  # noqa: BLE001 - sync must never crash the process
        summary.errors.append(str(exc))
        repo.record_sync_error(conn, user_id=user_id, source="classroom", error=str(exc))

    return summary


def _apply_upsert(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    course_id: int,
    course_external_id: str,
    course_code: str | None,
    source_type: str,
    item: dict[str, Any],
    kind: str,
    actual_deadline: str | None,
    summary: SyncSummary,
    context: _RelevanceContext | None = None,
) -> None:
    result = repo.upsert_task_from_source(
        conn,
        user_id=user_id,
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

    # Evaluated on every sync, not only on create/change: the decision also
    # depends on the user's enrollment profile, which can change without any
    # Classroom-side change. set_task_relevance skips the write when the
    # decision is unchanged, so a routine re-sync stays idempotent.
    _evaluate_relevance(
        conn, user_id=user_id, task_id=result.task_id, item=item, context=context, summary=summary
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
            _schedule_reminders(conn, user_id=user_id, task_id=result.task_id,
                                 title=item.get("title") or "",
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
        repo.cancel_pending_reminders(conn, user_id=user_id, task_id=result.task_id)
        # Queued after the cancel above, or it would be cancelled with the
        # stale countdown it is announcing. Keyed on the *new* deadline so
        # re-detecting the same change can never send twice, while a genuine
        # second change does produce a second alert.
        repo.insert_reminder_if_absent(
            conn,
            user_id=user_id,
            task_id=result.task_id,
            reminder_type="DEADLINE_CHANGED",
            scheduled_for=repo.now_iso(),
            idempotency_key=f"{result.task_id}:DEADLINE_CHANGED:{result.new_deadline}",
        )
        if result.new_deadline:
            _schedule_reminders(conn, user_id=user_id, task_id=result.task_id,
                                 title=item.get("title") or "",
                                 course_code=course_code, actual_deadline=result.new_deadline,
                                 discovered_at=repo.now_iso())


def _schedule_reminders(
    conn: sqlite3.Connection,
    *,
    user_id: int,
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
            user_id=user_id,
            task_id=task_id,
            reminder_type=plan_item.reminder_type,
            scheduled_for=scheduled_iso,
            idempotency_key=idempotency_key,
        )


def _sync_coursework(
    conn, client, user_id, course_id, course_external_id, summary, context=None
) -> set[str]:
    course_code = None
    items = client.list_course_work(course_external_id)
    for item in items:
        deadline = _classroom_due_to_iso(item.get("dueDate"), item.get("dueTime"))
        _apply_upsert(
            conn, user_id=user_id, course_id=course_id, course_external_id=course_external_id,
            course_code=course_code, source_type="coursework", item=item,
            kind="ACTIONABLE", actual_deadline=deadline, summary=summary, context=context,
        )
    return {item["id"] for item in items}


def _sync_announcements(
    conn, client, user_id, course_id, course_external_id, summary, context=None
) -> set[str]:
    items = client.list_announcements(course_external_id)
    for item in items:
        _apply_upsert(
            conn, user_id=user_id, course_id=course_id, course_external_id=course_external_id,
            course_code=None, source_type="announcement", item=item,
            kind="INFORMATIONAL", actual_deadline=None, summary=summary, context=context,
        )
    return {item["id"] for item in items}


def _sync_materials(
    conn, client, user_id, course_id, course_external_id, summary, context=None
) -> set[str]:
    items = client.list_course_materials(course_external_id)
    for item in items:
        _apply_upsert(
            conn, user_id=user_id, course_id=course_id, course_external_id=course_external_id,
            course_code=None, source_type="material", item=item,
            kind="INFORMATIONAL", actual_deadline=None, summary=summary, context=context,
        )
    return {item["id"] for item in items}
