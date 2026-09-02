"""Class-aware reminders: "DLD starts in ~30 min, C-311".

Occurrences are computed on demand from the weekly timetable pattern (see
ragra/timetable/schedule.py) - nothing about a future class is ever stored.
The only persistent state is the record that a particular occurrence has
already been announced, which is precisely the piece that has to survive a
restart to keep the reminder from firing twice.

Deliberately a *window*, not an instant. The scheduled tick runs every 15
minutes, so it can only ever notice a class within +/-15 minutes of any
chosen moment; promising "in exactly 30 minutes" would be a claim the
cadence cannot support. Instead every class starting within the lookahead
window is announced once, and the message states the real start time.

Delivery reuses the provider-neutral notification layer unchanged - this
module knows nothing about Hermes, email, or any other transport.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ragra.adapters.notify import Notification, NotificationProvider, send_to_all_providers
from ragra.db import repo
from ragra.timetable.schedule import expand_occurrences, weekly_class_from_row
from ragra.tz import parse_instant, to_local, utc_iso

CLASS_SOON = "CLASS_SOON"

# How far ahead a class is announced. Chosen against the 15-minute tick: a
# class is picked up on the first tick that falls inside this window, so the
# alert lands roughly 30-45 minutes ahead rather than at an exact offset.
LOOKAHEAD = timedelta(minutes=45)


@dataclass
class ClassReminderSummary:
    scheduled: int = 0
    sent: int = 0
    retrying: int = 0
    expired: int = 0
    failed: int = 0
    skipped_not_configured: int = 0
    errors: list[str] = field(default_factory=list)


def schedule_class_reminders(
    conn: sqlite3.Connection, *, now: datetime, lookahead: timedelta = LOOKAHEAD
) -> int:
    """Claim every not-yet-announced class starting within the lookahead
    window. Returns how many were newly claimed. Idempotent: re-running
    claims nothing new."""
    occurrences = expand_occurrences(
        [weekly_class_from_row(row) for row in repo.list_timetable_events(conn)],
        window_start=now,
        window_end=now + lookahead,
    )

    claimed = 0
    for occurrence in occurrences:
        # A cancelled class must never produce a reminder - the whole point
        # of tracking cancellations.
        if occurrence.is_cancelled:
            continue
        inserted = repo.insert_class_reminder_if_absent(
            conn,
            timetable_event_id=occurrence.timetable_event_id,
            occurrence_date=occurrence.occurrence_date.isoformat(),
            reminder_type=CLASS_SOON,
            scheduled_for=utc_iso(occurrence.starts_at_utc),
        )
        if inserted is not None:
            claimed += 1
    return claimed


def class_reminder_message(
    *, course_name: str, starts_at_utc: datetime, room: str | None, now: datetime
) -> str:
    minutes = max(0, int((starts_at_utc - now).total_seconds() // 60))
    local = to_local(starts_at_utc)
    where = f", {room}" if room else ""
    return (
        f"{course_name} starts in ~{minutes} min "
        f"({local.strftime('%I:%M %p').lstrip('0')} {local.strftime('%Z')}){where}"
    )


def dispatch_class_reminders(
    conn: sqlite3.Connection,
    *,
    providers: list[NotificationProvider],
    now: datetime,
) -> ClassReminderSummary:
    """Send every claimed class reminder whose class has not started yet."""
    summary = ClassReminderSummary()

    for row in repo.pending_class_reminders(conn):
        starts_at = parse_instant(row["scheduled_for"])

        # Already started: too late to be useful. Expire rather than send -
        # a "starts in ~0 min" alert for a class in progress is noise.
        if starts_at <= now:
            repo.record_class_reminder_attempt(
                conn, class_reminder_id=row["id"],
                error="class already started; reminder expired", give_up=True,
            )
            summary.expired += 1
            continue

        if not providers:
            # Nothing attempted, so nothing is consumed - it stays PENDING
            # and goes out as soon as a provider is configured.
            summary.skipped_not_configured += 1
            continue

        message = class_reminder_message(
            course_name=row["course_name"] or "Class",
            starts_at_utc=starts_at,
            room=row["room"],
            now=now,
        )
        def _record(provider_name: str, result) -> None:
            # No reminder_id: a class reminder has no row in `reminders`
            # (see migration 0003), but its delivery is still auditable.
            repo.record_notification_delivery(
                conn, provider=provider_name, ok=result.ok,
                category=CLASS_SOON, error=result.error,
            )

        delivered, errors = send_to_all_providers(
            providers, Notification(text=message, category=CLASS_SOON), on_attempt=_record
        )

        if delivered:
            repo.mark_class_reminder_sent(conn, class_reminder_id=row["id"])
            summary.sent += 1
            continue

        error = "; ".join(errors) or "unknown error"
        summary.errors.append(error)
        # Retry on the next tick while the class is still ahead; give up
        # once it has started.
        repo.record_class_reminder_attempt(
            conn, class_reminder_id=row["id"], error=error, give_up=False
        )
        summary.retrying += 1

    return summary


def run_class_reminders(
    conn: sqlite3.Connection,
    *,
    providers: list[NotificationProvider],
    now: datetime,
) -> ClassReminderSummary:
    """One full pass: expire anything stale, claim upcoming classes, deliver."""
    summary = ClassReminderSummary()
    summary.expired += repo.expire_stale_class_reminders(conn, now=utc_iso(now))
    summary.scheduled = schedule_class_reminders(conn, now=now)

    dispatched = dispatch_class_reminders(conn, providers=providers, now=now)
    summary.sent = dispatched.sent
    summary.retrying = dispatched.retrying
    summary.expired += dispatched.expired
    summary.skipped_not_configured = dispatched.skipped_not_configured
    summary.errors = dispatched.errors
    return summary
