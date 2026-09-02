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
from ragra.tz import format_local, format_stored_local, local_day_bounds, utc_iso


def _todays_classes(conn: sqlite3.Connection, *, now: datetime) -> list:
    """Today's classes, computed on demand from the weekly timetable
    pattern. Best-effort: a timetable problem (a malformed stored time, or
    missing timezone data) must not take the whole brief down, since every
    deadline fact in it is still correct and useful."""
    from ragra.timetable.schedule import occurrences_for_local_day, weekly_class_from_row

    try:
        rows = repo.list_timetable_events(conn)
        return occurrences_for_local_day(
            [weekly_class_from_row(row) for row in rows], instant=now
        )
    except Exception:  # noqa: BLE001 - the brief degrades, never fails
        return []


def build_deterministic_brief(conn: sqlite3.Connection, *, now: datetime) -> str:
    now_iso = utc_iso(now)
    # "Today" is the campus calendar day, not the UTC one. These differ for
    # five hours of every day, during which a UTC-day boundary silently
    # moved work into or out of "due today" - see ragra/tz.py.
    _day_start, day_end = local_day_bounds(now)
    end_of_today_iso = utc_iso(day_end)
    week_end_iso = utc_iso(now + timedelta(days=7))

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
        return f"  - {course}: {t['title']} (due {format_stored_local(t['actual_deadline'])})"

    lines = [f"Good morning. Here is your academic status as of {format_local(now)}.", ""]

    classes = _todays_classes(conn, now=now)
    lines.append(f"CLASSES TODAY ({len(classes)}):")
    if classes:
        for occurrence in classes:
            room = f" - {occurrence.room}" if occurrence.room else ""
            cancelled = " [CANCELLED]" if occurrence.is_cancelled else ""
            lines.append(
                f"  - {occurrence.starts_at_local.strftime('%H:%M')}"
                f"-{occurrence.ends_at_local.strftime('%H:%M')} "
                f"{occurrence.course_name}{room}{cancelled}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

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
            lines.append(
                f"  - [{r['reminder_type']}] {r['course_code']}: {r['task_title']} "
                f"at {format_stored_local(r['scheduled_for'])}"
            )
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def build_full_brief(conn: sqlite3.Connection, *, now: datetime, hermes_bin: Path | None) -> str:
    """Deterministic brief plus an optional AI priority narrative. The AI
    section is best-effort and never blocks or replaces the facts above -
    it's imported lazily here so a missing/broken AI package degrades to a
    clear note rather than an import failure."""
    text = build_deterministic_brief(conn, now=now)

    now_iso = utc_iso(now)
    week_end_iso = utc_iso(now + timedelta(days=7))
    try:
        from ragra.adapters.ai import AIAdapterError
        from ragra.ai.advisor import ask_for_priorities

        ai_notes = ask_for_priorities(conn, hermes_bin=hermes_bin, now_iso=now_iso, week_end_iso=week_end_iso)
    except ImportError as exc:
        return text + f"\n\n(AI priority notes unavailable - AI advisor not available: {exc})"
    except AIAdapterError as exc:
        return text + f"\n\n(AI priority notes unavailable: {exc})"

    return text + "\n\nAI PRIORITY NOTES (advisory only - facts above are authoritative):\n" + ai_notes
