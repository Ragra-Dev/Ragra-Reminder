"""Reminder dispatch: sends due, pending reminders through Hermes.

Idempotent: the dispatch query only ever selects PENDING reminders, and a
reminder is marked SENT only after a successful send - so re-running the
scheduler can never resend one that already went out.

Bounded retry: a send attempt that fails (not "unconfigured" - a genuine
delivery failure) doesn't immediately give up, and doesn't retry forever
either. It gets up to MAX_ATTEMPTS tries, spaced RETRY_DELAY apart, staying
PENDING (still a legitimate dispatch candidate) between attempts via
next_retry_at. Once attempts are exhausted the reminder transitions to the
terminal FAILED status - a genuinely permanent failure, visible in
`ragra reminders`/`tick` output and counted toward self-alerting (see
ragra/health.py). This never risks a duplicate send: exactly one delivery
attempt happens per dispatch pass per reminder, and a reminder leaves the
PENDING pool for good the moment either a send succeeds or attempts are
exhausted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ragra.adapters.notify import send_notification
from ragra.db import repo
from ragra.reminders.engine import reminder_message

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=15)


@dataclass
class DispatchSummary:
    sent: int = 0
    retrying: int = 0
    permanently_failed: int = 0
    skipped_not_configured: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """Backward-compatible alias: any non-success delivery attempt this
        pass (retrying or permanently failed)."""
        return self.retrying + self.permanently_failed


def preview_due_reminders(conn: sqlite3.Connection, *, now: str) -> list[dict]:
    """Non-destructive: shows exactly what dispatch_due_reminders would
    attempt to send right now - reminder type, message, and course/task -
    without sending anything or changing any reminder's status."""
    previews = []
    for reminder in repo.due_pending_reminders(conn, now=now):
        message = reminder_message(
            reminder["reminder_type"], reminder["task_title"], reminder["course_code"]
        )
        previews.append(
            {
                "reminder_id": reminder["id"],
                "reminder_type": reminder["reminder_type"],
                "scheduled_for": reminder["scheduled_for"],
                "message": message,
                "attempt_count": reminder["attempt_count"],
            }
        )
    return previews


def dispatch_due_reminders(
    conn: sqlite3.Connection,
    *,
    hermes_bin: Path | None,
    notify_target: str | None,
    now: str,
) -> DispatchSummary:
    summary = DispatchSummary()
    now_dt = datetime.fromisoformat(now)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    for reminder in repo.due_pending_reminders(conn, now=now):
        message = reminder_message(
            reminder["reminder_type"], reminder["task_title"], reminder["course_code"]
        )
        result = send_notification(hermes_bin=hermes_bin, target=notify_target, message=message)

        if result.ok:
            repo.mark_reminder_sent(conn, reminder_id=reminder["id"])
            summary.sent += 1
            continue

        if result.error == "notification delivery is not configured":
            # Left PENDING, attempt_count untouched: nothing was actually
            # attempted, so this doesn't consume retry budget - it's safe
            # (and correct) to try again once notify_target/hermes_bin are set.
            summary.skipped_not_configured += 1
            continue

        error = result.error or "unknown error"
        attempt = reminder["attempt_count"] + 1
        if attempt >= MAX_ATTEMPTS:
            repo.mark_reminder_failed(conn, reminder_id=reminder["id"], error=error, attempt_count=attempt)
            summary.permanently_failed += 1
        else:
            next_retry_at = (now_dt + RETRY_DELAY).isoformat()
            repo.mark_reminder_for_retry(
                conn, reminder_id=reminder["id"], error=error, attempt_count=attempt, next_retry_at=next_retry_at
            )
            summary.retrying += 1
        summary.errors.append(error)

    return summary
