"""Daily academic brief: overdue work, today's deadlines, upcoming
deadlines, and scheduled reminders - all deterministic, straight from
Ragra's own tables. The AI priority narrative (see ragra/ai/advisor.py) is
an optional feature, strictly additive: `build_deterministic_brief` has no
dependency on it at all, and `build_full_brief` imports it lazily so the
rest of this module - and anything that merely imports it - keeps working
even with the AI package unavailable. If AI is unavailable or fails, the
brief still prints in full with everything factual intact, plus a short
note explaining why the AI section is missing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ragra.db import repo


def build_deterministic_brief(conn: sqlite3.Connection, *, now: datetime) -> str:
    now_iso = now.isoformat()
    end_of_today_iso = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    week_end_iso = (now + timedelta(days=7)).isoformat()

    overdue = repo.overdue_tasks(conn, now=now_iso)
    due_today = repo.tasks_due_between(conn, start_iso=now_iso, end_iso=end_of_today_iso)
    due_today_ids = {t["id"] for t in due_today}
    due_soon = [
        t for t in repo.tasks_due_between(conn, start_iso=now_iso, end_iso=week_end_iso)
        if t["id"] not in due_today_ids
    ]
    reminders_today = [
        r for r in repo.upcoming_scheduled_reminders(conn, now=now_iso, limit=100)
        if r["scheduled_for"] <= end_of_today_iso
    ]

    def _line(t: sqlite3.Row) -> str:
        course = t["course_code"] or t["course_name"]
        return f"  - {course}: {t['title']} (due {t['actual_deadline']})"

    lines = [f"Good morning. Here is your academic status as of {now_iso}.", ""]

    lines.append(f"OVERDUE ({len(overdue)}):")
    if overdue:
        lines.extend(_line(t) for t in overdue)
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"DUE TODAY ({len(due_today)}):")
    if due_today:
        lines.extend(_line(t) for t in due_today)
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"DUE SOON, next 7 days ({len(due_soon)}):")
    if due_soon:
        lines.extend(_line(t) for t in due_soon)
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"REMINDERS FIRING TODAY ({len(reminders_today)}):")
    if reminders_today:
        for r in reminders_today:
            lines.append(f"  - [{r['reminder_type']}] {r['course_code']}: {r['task_title']} at {r['scheduled_for']}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def build_full_brief(conn: sqlite3.Connection, *, now: datetime, hermes_bin: Path | None) -> str:
    """Deterministic brief plus an optional AI priority narrative. The AI
    section is best-effort and never blocks or replaces the facts above -
    it's imported lazily here so a missing/broken AI package degrades to a
    clear note rather than an import failure."""
    text = build_deterministic_brief(conn, now=now)

    now_iso = now.isoformat()
    week_end_iso = (now + timedelta(days=7)).isoformat()
    try:
        from ragra.adapters.ai import AIAdapterError
        from ragra.ai.advisor import ask_for_priorities

        ai_notes = ask_for_priorities(conn, hermes_bin=hermes_bin, now_iso=now_iso, week_end_iso=week_end_iso)
    except ImportError as exc:
        return text + f"\n\n(AI priority notes unavailable - AI advisor not available: {exc})"
    except AIAdapterError as exc:
        return text + f"\n\n(AI priority notes unavailable: {exc})"

    return text + "\n\nAI PRIORITY NOTES (advisory only - facts above are authoritative):\n" + ai_notes
