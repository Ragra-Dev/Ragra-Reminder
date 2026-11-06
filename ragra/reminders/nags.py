"""Repeat an alert inside the app until the person presses "Received".

Every alert that reaches the in-app chat starts a repeat clock. Each pass of
the tick adds a fresh "Reminder" message to the chat for every alert that is
due, then pushes that alert's next repeat out by NAG_INTERVAL. It never stops
by itself: only pressing Received (on the alert or any of its repeats) does.
Repeats stay in the app - they are not sent to email or WhatsApp.

The real gap is the interval rounded up to the next tick
(RAGRA_TICK_INTERVAL_SECONDS).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from ragra.db import repo

NAG_INTERVAL = timedelta(minutes=5)


def next_nag_at(now: datetime) -> str:
    return (now + NAG_INTERVAL).isoformat()


def run_nags(
    conn: sqlite3.Connection, *, user_id: int, now: datetime | None = None
) -> int:
    """Post a repeat for each of this user's due, unacknowledged alerts.
    Returns how many were posted."""
    now = now or datetime.now(timezone.utc)
    posted = 0
    for message in repo.due_nags(conn, user_id=user_id, now=now.isoformat(), limit=20):
        repo.add_chat_message(
            conn,
            user_id=user_id,
            role="alert",
            text=f"Reminder - press Received to stop these.\n{message['text']}",
            link_path=message["link_path"],
            repeat_of=message["id"],
        )
        repo.bump_nag(conn, user_id=user_id, message_id=message["id"], next_at=next_nag_at(now))
        posted += 1
    return posted
