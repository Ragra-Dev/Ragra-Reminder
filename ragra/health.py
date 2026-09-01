"""Self-alerting: tracks consecutive failures per pipeline component
(classroom, calendar, reminders, tick) and sends ONE notification through
whichever notification provider(s) are configured (see
ragra/adapters/notify.py's NotificationProvider protocol - this module
never knows Hermes, WhatsApp, Web Push, or email specifically) when a
persisting failure streak crosses a threshold - never repeatedly for the
same ongoing outage. Uses the same multi-provider fan-out as
ragra/reminders/dispatch.py deliberately: this is the "something is wrong"
channel, so it shouldn't share a single point of failure with the thing
it's alerting about.

This is not a general monitoring system - it is a single small table
(pipeline_health) plus two functions. A healthy run resets a component's
streak to zero and re-arms alerting for a future failure; a failed run
increments the streak. Once the streak reaches FAILURE_ALERT_THRESHOLD, the
next check sends one alert and records last_alert_sent_at so it is not
sent again until the component recovers (streak resets) and later fails
again.
"""

from __future__ import annotations

import sqlite3

from ragra.adapters.notify import Notification, NotificationProvider, send_to_all_providers
from ragra.db import repo

# Three consecutive failed 15-minute ticks (~45 minutes) before alerting -
# long enough to ignore a single transient blip, short enough that a real
# outage doesn't go unnoticed for the day.
FAILURE_ALERT_THRESHOLD = 3


def record_result(conn: sqlite3.Connection, *, component: str, success: bool, error: str | None = None) -> int:
    """Updates the component's streak. Returns the resulting
    consecutive_failures count (0 after a success)."""
    now = repo.now_iso()
    if success:
        conn.execute(
            """INSERT INTO pipeline_health (component, consecutive_failures, last_success_at, last_alert_sent_at)
               VALUES (?, 0, ?, NULL)
               ON CONFLICT(component) DO UPDATE SET
                 consecutive_failures = 0,
                 last_success_at = excluded.last_success_at,
                 last_alert_sent_at = NULL""",
            (component, now),
        )
        conn.commit()
        return 0

    conn.execute(
        """INSERT INTO pipeline_health (component, consecutive_failures, last_failure_at, last_error)
           VALUES (?, 1, ?, ?)
           ON CONFLICT(component) DO UPDATE SET
             consecutive_failures = consecutive_failures + 1,
             last_failure_at = excluded.last_failure_at,
             last_error = excluded.last_error""",
        (component, now, error or "(no error detail)"),
    )
    conn.commit()
    row = conn.execute("SELECT consecutive_failures FROM pipeline_health WHERE component = ?", (component,)).fetchone()
    return row["consecutive_failures"]


def check_and_alert(conn: sqlite3.Connection, *, providers: list[NotificationProvider]) -> list[str]:
    """Sends at most one combined alert for every component that just
    crossed the threshold and hasn't already been alerted for this streak.
    Returns the component names actually alerted (empty if nothing crossed
    the threshold, no provider is configured, or every configured provider's
    send failed - in which case it is retried on a future call, not lost)."""
    if not providers:
        return []

    rows = conn.execute(
        "SELECT * FROM pipeline_health WHERE consecutive_failures >= ? AND last_alert_sent_at IS NULL",
        (FAILURE_ALERT_THRESHOLD,),
    ).fetchall()
    if not rows:
        return []

    lines = [
        f"- {r['component']}: failing {r['consecutive_failures']} consecutive runs ({r['last_error'] or 'no error detail'})"
        for r in rows
    ]
    message = "Ragra health alert - needs attention:\n" + "\n".join(lines)
    notification = Notification(text=message, category="HEALTH_ALERT")

    delivered, _errors = send_to_all_providers(providers, notification)
    if not delivered:
        # Couldn't deliver the alert through any configured provider - leave
        # last_alert_sent_at unset so the next check tries again, rather
        # than silently losing it.
        return []

    now = repo.now_iso()
    alerted = []
    for r in rows:
        conn.execute("UPDATE pipeline_health SET last_alert_sent_at = ? WHERE component = ?", (now, r["component"]))
        alerted.append(r["component"])
    conn.commit()
    return alerted
