"""Ragra dashboard: FastAPI app.

Every route resolves the acting user through `current_user_id()` and passes
that id explicitly into the repository layer. Nothing here reads or writes
a row without naming its owner - which is what makes the ownership filter,
not the URL, decide what a request can see. Requesting another user's task
id returns 404 from the scoped query rather than the row.

Identity resolution is intentionally minimal for now: the single
pre-identity owner row. Real sign-in (sessions, Google OAuth) is a
follow-up piece that replaces `current_user_id()`'s body, not its callers.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.timetable.schedule import occurrences_for_local_day, weekly_class_from_row
from ragra.tz import format_stored_local, local_day_bounds, utc_iso

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class NoCurrentUser(RuntimeError):
    """No acting user could be resolved. Raised rather than defaulting to
    "the first user": silently picking an owner is how one account ends up
    reading another's data."""


def current_user_id(conn: sqlite3.Connection, request: Request | None = None) -> int:
    """The user this request acts as.

    Deliberately fails closed: if there is no unambiguous pre-identity
    owner row, this raises instead of guessing, and the route returns 500
    rather than operating on somebody's data by accident.
    """
    user_id = repo.unlinked_user_id(conn)
    if user_id is None:
        raise NoCurrentUser("no unambiguous current user; sign-in is required")
    return user_id


def _todays_classes(conn, *, user_id: int, now: datetime) -> list:
    """Today's classes, computed on demand from the weekly pattern. Failing
    softly on purpose: a timetable problem must not blank the whole
    dashboard, whose deadline data is unaffected and still correct."""
    try:
        rows = repo.list_timetable_events(conn, user_id=user_id)
        return occurrences_for_local_day(
            [weekly_class_from_row(row) for row in rows], instant=now
        )
    except Exception:  # noqa: BLE001 - the schedule section degrades, the page does not
        return []

# How many MISSED tasks the main dashboard shows before pointing to the
# full list. Not a "historical" classification (see docs/ARCHITECTURE.md
# discussion) - purely a display limit, most-recent-deadline-first, so a
# long tail of old work doesn't dominate the daily view. Everything stays
# reachable via /missed regardless.
MISSED_SECTION_PREVIEW_LIMIT = 5

# Same rationale as the missed preview: the dashboard shows the most recent
# untriaged announcements, with the full list one click away.
ANNOUNCEMENT_PREVIEW_LIMIT = 5


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="Ragra")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.get("/")
    def today(request: Request):
        now = datetime.now(timezone.utc)
        now_iso = utc_iso(now)
        # The campus calendar day, not the UTC one - see ragra/tz.py. These
        # disagree for five hours daily, during which a UTC boundary moves
        # work into or out of "due today".
        _day_start, day_end = local_day_bounds(now)
        end_of_today_iso = utc_iso(day_end)
        week_end_iso = utc_iso(now + timedelta(days=7))

        with connect_closing(db_path) as conn:
            user_id = current_user_id(conn, request)
            classes_today = _todays_classes(conn, user_id=user_id, now=now)
            announcements = repo.open_announcements(
                conn, user_id=user_id, limit=ANNOUNCEMENT_PREVIEW_LIMIT
            )
            personal_tasks = repo.manual_tasks(conn, user_id=user_id)
            overdue = repo.overdue_tasks(conn, user_id=user_id, now=now_iso)
            missed = repo.missed_tasks(
                conn, user_id=user_id, limit=MISSED_SECTION_PREVIEW_LIMIT
            )
            missed_total = repo.count_missed_tasks(conn, user_id=user_id)
            due_today = repo.tasks_due_between(
                conn, user_id=user_id, start_iso=now_iso, end_iso=end_of_today_iso
            )
            due_today_ids = {t["id"] for t in due_today}
            due_soon = [
                t for t in repo.tasks_due_between(
                    conn, user_id=user_id, start_iso=now_iso, end_iso=week_end_iso
                )
                if t["id"] not in due_today_ids
            ]
            needs_planning = conn.execute(
                """SELECT tasks.*, courses.course_code, courses.name AS course_name
                   FROM tasks JOIN courses ON courses.id = tasks.course_id
                   WHERE tasks.user_id = ? AND kind = 'ACTIONABLE' AND actual_deadline IS NULL
                   AND personal_deadline IS NULL AND status NOT IN ('COMPLETED', 'CANCELLED')
                   ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()
            needs_personal_target = repo.tasks_missing_personal_target(conn, user_id=user_id)
            scheduled_reminders = repo.upcoming_scheduled_reminders(
                conn, user_id=user_id, now=now_iso
            )
            recently_completed = repo.recently_completed_tasks(conn, user_id=user_id)

        priority = due_today[0] if due_today else (overdue[0] if overdue else (due_soon[0] if due_soon else None))

        return templates.TemplateResponse(
            request,
            "today.html",
            {
                "overdue": overdue,
                "missed": missed,
                "missed_total": missed_total,
                "missed_preview_limit": MISSED_SECTION_PREVIEW_LIMIT,
                "due_today": due_today,
                "due_soon": due_soon,
                "needs_planning": needs_planning,
                "needs_personal_target": needs_personal_target,
                "scheduled_reminders": scheduled_reminders,
                "recently_completed": recently_completed,
                "priority": priority,
                "classes_today": classes_today,
                "announcements": announcements,
                "personal_tasks": personal_tasks,
            },
        )

    @app.get("/missed")
    def missed_full(request: Request):
        with connect_closing(db_path) as conn:
            # No limit - this user's full list, still only this user's.
            missed = repo.missed_tasks(conn, user_id=current_user_id(conn, request))

        return templates.TemplateResponse(request, "missed.html", {"missed": missed})

    @app.get("/brief")
    def brief(request: Request):
        from fastapi.responses import PlainTextResponse

        from ragra.brief import build_deterministic_brief

        with connect_closing(db_path) as conn:
            text = build_deterministic_brief(
                conn, user_id=current_user_id(conn, request), now=datetime.now(timezone.utc)
            )
        return PlainTextResponse(text)

    @app.get("/tasks/{task_id}")
    def task_detail(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            user_id = current_user_id(conn, request)
            # Scoped lookup: another user's task id is indistinguishable from
            # a nonexistent one, so this is a 404 and not a disclosure.
            task = repo.get_task_by_id(conn, user_id=user_id, task_id=task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            reminders = repo.reminders_for_task(conn, user_id=user_id, task_id=task_id)
            history = repo.history_for_task(conn, user_id=user_id, task_id=task_id)

        return templates.TemplateResponse(
            request,
            "task_detail.html",
            {"task": task, "reminders": reminders, "history": history},
        )

    @app.post("/tasks/{task_id}/complete")
    def complete_task(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            user_id = current_user_id(conn, request)
            repo.mark_completed(conn, user_id=user_id, task_id=task_id)
            repo.cancel_pending_reminders(conn, user_id=user_id, task_id=task_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{task_id}/personal-deadline")
    def set_personal_deadline(
        request: Request, task_id: int, personal_deadline: str = Form(...)
    ):
        # Allowed on every task, Classroom-sourced included: a personal
        # completion target is Ragra-owned data about the user's own plan and
        # never touches anything Classroom is authoritative for. See
        # docs/INTERFACES.md contract #5.
        with connect_closing(db_path) as conn:
            repo.set_personal_deadline(
                conn, user_id=current_user_id(conn, request), task_id=task_id,
                personal_deadline=personal_deadline,
            )
        return RedirectResponse("/", status_code=303)

    # --- Manual tasks -----------------------------------------------------
    #
    # Every route below declares each accepted form field by name. That is
    # the first and most important layer of the write guard: a route that
    # does not declare `title` structurally cannot receive one, so no amount
    # of extra POST data can reach a Classroom-authoritative column. The
    # repo-layer TaskSourceViolation check is the second layer, and input
    # validation is the third. There is no auth layer behind any of this.

    @app.get("/tasks")
    def tasks_page(request: Request):
        with connect_closing(db_path) as conn:
            personal = repo.manual_tasks(conn, user_id=current_user_id(conn, request))
        return templates.TemplateResponse(request, "tasks.html", {"tasks": personal})

    @app.post("/tasks/new")
    def create_task(
        request: Request,
        title: str = Form(...),
        description: str = Form(""),
        actual_deadline: str = Form(""),
        personal_deadline: str = Form(""),
    ):
        with connect_closing(db_path) as conn:
            try:
                repo.create_manual_task(
                    conn,
                    user_id=current_user_id(conn, request),
                    title=title,
                    description=description or None,
                    actual_deadline=_clean_deadline(actual_deadline),
                    personal_deadline=_clean_deadline(personal_deadline),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/edit")
    def edit_task(
        request: Request,
        task_id: int,
        title: str = Form(...),
        description: str = Form(""),
        actual_deadline: str = Form(""),
    ):
        with connect_closing(db_path) as conn:
            try:
                repo.update_manual_task(
                    conn,
                    user_id=current_user_id(conn, request),
                    task_id=task_id,
                    title=title,
                    description=description or None,
                    actual_deadline=_clean_deadline(actual_deadline),
                )
            except repo.TaskSourceViolation as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            user_id = current_user_id(conn, request)
            try:
                repo.cancel_task(conn, user_id=user_id, task_id=task_id)
            except repo.TaskSourceViolation as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            repo.cancel_pending_reminders(conn, user_id=user_id, task_id=task_id)
        return RedirectResponse("/tasks", status_code=303)

    # --- Announcements ----------------------------------------------------
    #
    # Fully deterministic: open it, optionally turn it into a task you own,
    # or archive it. No AI anywhere in this path - the announcement text is
    # never summarised, interpreted, or used to invent a deadline.

    @app.get("/announcements")
    def announcements_page(request: Request):
        with connect_closing(db_path) as conn:
            rows = repo.open_announcements(conn, user_id=current_user_id(conn, request))
        return templates.TemplateResponse(request, "announcements.html", {"announcements": rows})

    @app.post("/announcements/{task_id}/create-task")
    def create_task_from_announcement(
        request: Request,
        task_id: int,
        title: str = Form(""),
        actual_deadline: str = Form(""),
        personal_deadline: str = Form(""),
    ):
        with connect_closing(db_path) as conn:
            try:
                repo.create_task_from_announcement(
                    conn,
                    user_id=current_user_id(conn, request),
                    announcement_task_id=task_id,
                    title=title.strip() or None,
                    actual_deadline=_clean_deadline(actual_deadline),
                    personal_deadline=_clean_deadline(personal_deadline),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/announcements", status_code=303)

    @app.post("/announcements/{task_id}/archive")
    def archive_announcement(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            repo.archive_task(
                conn, user_id=current_user_id(conn, request), task_id=task_id
            )
        return RedirectResponse("/announcements", status_code=303)

    @app.get("/deliveries")
    def deliveries(request: Request):
        with connect_closing(db_path) as conn:
            rows = repo.recent_notification_deliveries(
                conn, user_id=current_user_id(conn, request), limit=100
            )
        return templates.TemplateResponse(request, "deliveries.html", {"deliveries": rows})

    return app


def _clean_deadline(value: str) -> str | None:
    """Empty means "no deadline", never "now". A supplied value must parse
    as a real date/datetime - storing an unparseable string would produce a
    row that every deadline comparison silently mis-sorts."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a valid date or date-time") from exc
    return text


def _default_db_path() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    home = Path(local_appdata) / "ragra" if local_appdata else Path.home() / ".ragra"
    return home / "ragra.db"


# Module-level ASGI app so `uvicorn ragra.web.app:app` (and
# `python -m ragra.web.app`, below) can find an entry point. Uses a fixed
# default database location; ragra/config.py will take over resolving this
# once the CLI entrypoint is wired up.
app = create_app(_default_db_path())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8731)
