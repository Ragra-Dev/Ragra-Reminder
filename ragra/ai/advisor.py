"""AI advisory layer: priority reasoning, workload framing, "what should I
work on next," deadline-risk explanation.

Hard boundary (see docs/ARCHITECTURE.md's AI boundary): the AI is handed a
read-only, deterministically-built snapshot of Ragra's own data and asked
to reason ABOUT it. It never invents facts (the prompt explicitly forbids
it), and its output is never written back into tasks/deadlines/reminders -
build_snapshot_prompt() is pure and independently testable without any AI
call, and ask_for_priorities()'s return value is display-only text.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ragra.adapters.ai import ask
from ragra.db import repo


def build_snapshot_prompt(
    conn: sqlite3.Connection, *, user_id: int, now_iso: str, week_end_iso: str
) -> str:
    """Pure: assembles a deterministic, factual snapshot of overdue/due-soon
    work and asks for a prioritized plan. No network call happens here.

    Scoped to one user, and that matters more here than in a display path:
    the snapshot leaves Ragra entirely, so an unscoped query would send one
    student's coursework to a model on another student's behalf."""
    overdue = repo.overdue_tasks(conn, user_id=user_id, now=now_iso)
    due_soon = repo.tasks_due_between(
        conn, user_id=user_id, start_iso=now_iso, end_iso=week_end_iso
    )
    missing_target = repo.tasks_missing_personal_target(conn, user_id=user_id)

    def _line(t: sqlite3.Row) -> str:
        course = t["course_code"] or t["course_name"]
        personal = f", personal target {t['personal_deadline']}" if t["personal_deadline"] else ""
        return f"- {course}: {t['title']} (actual deadline {t['actual_deadline']}{personal})"

    overdue_block = "\n".join(_line(t) for t in overdue) or "(none)"
    due_soon_block = "\n".join(_line(t) for t in due_soon) or "(none)"
    missing_target_block = "\n".join(_line(t) for t in missing_target) or "(none)"

    return (
        "You are a study-planning assistant. Below is the COMPLETE and ONLY "
        "factual data you have about the student's academic workload. Do not "
        "invent, assume, or reference any assignment, course, or deadline that "
        "is not explicitly listed below - if something is missing, say so "
        "instead of guessing.\n\n"
        f"OVERDUE (already past deadline):\n{overdue_block}\n\n"
        f"DUE WITHIN 7 DAYS:\n{due_soon_block}\n\n"
        f"HAS A DEADLINE BUT NO PERSONAL COMPLETION TARGET SET YET:\n{missing_target_block}\n\n"
        "Using only the facts above, write a short, practical response with "
        "three parts:\n"
        "1) PRIORITY ORDER - what to work on first and why, in one line each.\n"
        "2) DEADLINE RISK - flag anything genuinely at risk of being missed, "
        "and why (e.g. overdue, or due soon with no personal plan yet).\n"
        "3) SUGGESTED PLAN - a brief, realistic plan for today given the above.\n"
        "Keep the whole response under 200 words. Do not restate the raw list."
    )


def ask_for_priorities(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    hermes_bin: Path | None,
    now_iso: str,
    week_end_iso: str,
) -> str:
    prompt = build_snapshot_prompt(
        conn, user_id=user_id, now_iso=now_iso, week_end_iso=week_end_iso
    )
    return ask(hermes_bin, prompt)
