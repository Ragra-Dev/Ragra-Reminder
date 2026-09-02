"""Reminder dispatch: sends due, pending reminders through whichever
notification provider(s) are configured (see ragra/adapters/notify.py's
NotificationProvider protocol). This module never imports or knows about
Hermes, WhatsApp, Web Push, or email specifically - only `send(message)`.

Idempotent: the dispatch query only ever selects PENDING reminders, and a
reminder is marked SENT only after a successful send - so re-running the
scheduler can never resend one that already went out.

Multi-provider delivery: every configured provider is attempted; a reminder
is marked SENT if AT LEAST ONE succeeds. This is deliberate redundancy, not
just a config convenience - with two independent providers configured, one
channel breaking (e.g. Hermes) no longer silently takes down all reminder
delivery. An empty provider list is a normal, fully-supported state (core
Ragra never requires one) - reminders simply stay PENDING.

Bounded retry: a send attempt where every configured provider fails (not
"unconfigured" - a genuine delivery failure) doesn't immediately give up,
and doesn't retry forever either. It gets up to MAX_ATTEMPTS tries, spaced
RETRY_DELAY apart, staying PENDING (still a legitimate dispatch candidate)
between attempts via next_retry_at. Once attempts are exhausted the
reminder transitions to the terminal FAILED status - a genuinely permanent
failure, visible in `ragra reminders`/`tick` output and counted toward
self-alerting (see ragra/health.py). This never risks a duplicate send:
exactly one delivery attempt happens per dispatch pass per reminder per
provider, and a reminder leaves the PENDING pool for good the moment either
a send succeeds or attempts are exhausted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ragra.adapters.notify import Notification, NotificationProvider, send_to_all_providers
from ragra.db import repo
from ragra.reminders.engine import reminder_message

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=15)


def _delivery_recorder(conn: sqlite3.Connection, notification: Notification):
    """Per-provider delivery recording. Lives here rather than in
    ragra/adapters/notify.py so the notification layer never touches the
    database - see docs/INTERFACES.md contract #1."""

    def record(provider_name: str, result) -> None:
        repo.record_notification_delivery(
            conn,
            provider=provider_name,
            ok=result.ok,
            reminder_id=notification.reminder_id,
            category=notification.category,
            error=result.error,
        )

    return record


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
    providers: list[NotificationProvider],
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

        if not providers:
            # Left PENDING, attempt_count untouched: nothing was actually
            # attempted, so this doesn't consume retry budget - it's safe
            # (and correct) to try again once a provider is configured.
            summary.skipped_not_configured += 1
            continue

        notification = Notification(
            text=message, reminder_id=reminder["id"], category=reminder["reminder_type"]
        )
        delivered, errors = send_to_all_providers(
            providers, notification, on_attempt=_delivery_recorder(conn, notification)
        )

        if delivered:
            repo.mark_reminder_sent(conn, reminder_id=reminder["id"])
            summary.sent += 1
            continue

        error = "; ".join(errors) or "unknown error"
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
