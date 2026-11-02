"""The WhatsApp assistant: turns a person's message into an answer about their
own coursework.

Plain commands ("pending", "today", "overdue", "week", "news", "help") are
answered straight from the database - deterministic, free and instant. Any
other question goes to the AI model together with a read-only snapshot of that
one person's pending work, and the prompt forbids inventing anything outside
it. The model never gets a tool or a write path; its answer is only text.

Everything is scoped by `user_id`: the number that wrote in identifies exactly
one account (see repo.user_id_for_whatsapp) and only that account's rows are
read.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from ragra.adapters.ai import AIAdapterError
from ragra.adapters.gemini import ask_gemini
from ragra.config import Config
from ragra.db import repo

HELP_TEXT = (
    "Hi! I'm Ragra. Ask me about your Classroom work:\n"
    "- pending - everything you still have to do\n"
    "- today - what is due today\n"
    "- overdue - what you have missed the deadline on\n"
    "- week - due in the next 7 days\n"
    "- news - the latest announcements\n"
    "Or just ask a question, e.g. \"what should I do first?\""
)

_PENDING = {"pending", "tasks", "task", "assignments", "assignment", "todo", "to do", "to-do", "left"}
_OVERDUE = {"overdue", "late", "missed"}
_TODAY = {"today", "due today"}
_WEEK = {"week", "soon", "upcoming", "this week", "next 7 days"}
_NEWS = {"news", "announcements", "announcement", "updates"}
_HELP = {"help", "hi", "hello", "hey", "start", "menu", "salam", "assalamualaikum"}


def _when(iso: str | None) -> str:
    if not iso:
        return "no deadline"
    try:
        from ragra.tz import parse_instant, to_local

        local = to_local(parse_instant(iso))
    except Exception:  # noqa: BLE001 - a label must never break an answer
        return iso
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a')} {local.day} {local.strftime('%b')}, {hour}:{local.strftime('%M %p').lower()}"


def _line(task: sqlite3.Row) -> str:
    course = task["course_code"] or task["course_name"]
    return f"- {course}: {task['title']} (due {_when(task['actual_deadline'])})"


def _listing(title: str, tasks: list[sqlite3.Row], empty: str) -> str:
    if not tasks:
        return empty
    shown = tasks[:12]
    more = f"\n...and {len(tasks) - len(shown)} more" if len(tasks) > len(shown) else ""
    return f"{title} ({len(tasks)}):\n" + "\n".join(_line(t) for t in shown) + more


def snapshot_prompt(conn: sqlite3.Connection, *, user_id: int, question: str, now: datetime) -> str:
    pending = repo.pending_work(conn, user_id=user_id)
    lines = "\n".join(_line(t) for t in pending) or "(nothing pending)"
    return (
        "You are Ragra, a concise study assistant replying on WhatsApp to one student.\n"
        "Answer ONLY from the data below. If the answer is not in the data, say you do not know. "
        "Never invent courses, tasks or deadlines. Keep it short, friendly and plain text "
        "(no markdown tables).\n\n"
        f"Current time: {_when(now.isoformat())}\n"
        f"The student's pending work (soonest first):\n{lines}\n\n"
        f"Student's question: {question}"
    )


def answer(
    conn: sqlite3.Connection,
    config: Config,
    *,
    user_id: int,
    text: str,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    key = " ".join(text.lower().replace("?", " ").replace("!", " ").replace(".", " ").split())

    if key in _HELP or not key:
        return HELP_TEXT
    if key in _OVERDUE:
        return _listing("Overdue", repo.overdue_tasks(conn, user_id=user_id, now=now_iso), "Nothing overdue.")
    if key in _TODAY:
        end = now.astimezone().replace(hour=23, minute=59, second=59).astimezone(timezone.utc).isoformat()
        due = repo.tasks_due_between(conn, user_id=user_id, start_iso=now_iso, end_iso=end)
        return _listing("Due today", due, "Nothing else is due today.")
    if key in _WEEK:
        end = (now + timedelta(days=7)).isoformat()
        due = repo.tasks_due_between(conn, user_id=user_id, start_iso=now_iso, end_iso=end)
        return _listing("Due in the next 7 days", due, "Nothing due in the next 7 days.")
    if key in _PENDING:
        return _listing("Pending", repo.pending_work(conn, user_id=user_id), "You have nothing pending.")
    if key in _NEWS:
        items = repo.open_announcements(conn, user_id=user_id, limit=5)
        if not items:
            return "No new announcements."
        return "Latest announcements:\n" + "\n".join(
            f"- {a['course_code'] or a['course_name']}: {(a['title'] or '').strip()[:140]}" for a in items
        )

    # Anything else is a free-form question for the model.
    if not config.gemini_api_key:
        return HELP_TEXT
    try:
        return ask_gemini(
            config.gemini_api_key,
            config.gemini_model,
            snapshot_prompt(conn, user_id=user_id, question=text.strip()[:500], now=now),
        )
    except AIAdapterError:
        return "Sorry, I couldn't think of an answer just now. Try 'pending', 'today' or 'overdue'."
