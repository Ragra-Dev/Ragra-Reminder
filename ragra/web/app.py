"""Ragra dashboard: FastAPI app. Single user, no auth (localhost only)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ragra.db import repo
from ragra.db.connection import connect_closing

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# How many MISSED tasks the main dashboard shows before pointing to the
# full list. Not a "historical" classification (see docs/ARCHITECTURE.md
# discussion) - purely a display limit, most-recent-deadline-first, so a
# long tail of old work doesn't dominate the daily view. Everything stays
# reachable via /missed regardless.
MISSED_SECTION_PREVIEW_LIMIT = 5


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="Ragra")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.get("/")
    def today(request: Request):
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        end_of_today_iso = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        week_end_iso = (now + timedelta(days=7)).isoformat()

        with connect_closing(db_path) as conn:
            overdue = repo.overdue_tasks(conn, now=now_iso)
            missed = repo.missed_tasks(conn, limit=MISSED_SECTION_PREVIEW_LIMIT)
            missed_total = repo.count_missed_tasks(conn)
            due_today = repo.tasks_due_between(conn, start_iso=now_iso, end_iso=end_of_today_iso)
            due_today_ids = {t["id"] for t in due_today}
            due_soon = [
                t for t in repo.tasks_due_between(conn, start_iso=now_iso, end_iso=week_end_iso)
                if t["id"] not in due_today_ids
            ]
            needs_planning = conn.execute(
                """SELECT tasks.*, courses.course_code, courses.name AS course_name
                   FROM tasks JOIN courses ON courses.id = tasks.course_id
                   WHERE kind = 'ACTIONABLE' AND actual_deadline IS NULL
                   AND personal_deadline IS NULL AND status NOT IN ('COMPLETED', 'CANCELLED')
                   ORDER BY created_at DESC"""
            ).fetchall()
            needs_personal_target = repo.tasks_missing_personal_target(conn)
            scheduled_reminders = repo.upcoming_scheduled_reminders(conn, now=now_iso)
            recently_completed = repo.recently_completed_tasks(conn)

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
            },
        )

    @app.get("/missed")
    def missed_full(request: Request):
        with connect_closing(db_path) as conn:
            missed = repo.missed_tasks(conn)  # no limit - the full, unfiltered list

        return templates.TemplateResponse(request, "missed.html", {"missed": missed})

    @app.get("/brief")
    def brief():
        from fastapi.responses import PlainTextResponse

        from ragra.brief import build_deterministic_brief

        with connect_closing(db_path) as conn:
            text = build_deterministic_brief(conn, now=datetime.now(timezone.utc))
        return PlainTextResponse(text)

    @app.get("/tasks/{task_id}")
    def task_detail(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            task = repo.get_task_by_id(conn, task_id=task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            reminders = repo.reminders_for_task(conn, task_id=task_id)
            history = repo.history_for_task(conn, task_id=task_id)

        return templates.TemplateResponse(
            request,
            "task_detail.html",
            {"task": task, "reminders": reminders, "history": history},
        )

    @app.post("/tasks/{task_id}/complete")
    def complete_task(task_id: int):
        with connect_closing(db_path) as conn:
            repo.mark_completed(conn, task_id=task_id)
            repo.cancel_pending_reminders(conn, task_id=task_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{task_id}/personal-deadline")
    def set_personal_deadline(task_id: int, personal_deadline: str = Form(...)):
        with connect_closing(db_path) as conn:
            repo.set_personal_deadline(conn, task_id=task_id, personal_deadline=personal_deadline)
        return RedirectResponse("/", status_code=303)

    return app


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
