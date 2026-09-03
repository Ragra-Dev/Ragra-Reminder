"""Small helpers shared by the test suite."""

from __future__ import annotations

import sqlite3

from ragra.db import repo


def owner_id(conn: sqlite3.Connection) -> int:
    """The user a single-user test acts as.

    Migration 0009 seeds exactly one user, and migrations 0010-0020 gave
    every pre-existing row to it, so this is the same owner a migrated
    production database's data belongs to. Tests that only care about
    behaviour (not isolation) use this so they exercise the real ownership
    path rather than inventing a test-only user; tests that care about
    isolation create a second user explicitly and assert across the two.
    """
    user_id = repo.unlinked_user_id(conn)
    assert user_id is not None, "expected exactly one seeded user in a fresh test database"
    return user_id


def make_user(conn: sqlite3.Connection, *, google_sub: str, display_name: str = "Test user") -> int:
    """Create an additional, fully independent account."""
    now = repo.now_iso()
    cur = conn.execute(
        """INSERT INTO users (google_sub, email, display_name, created_at, updated_at)
           VALUES (?, NULL, ?, ?, ?)""",
        (google_sub, display_name, now, now),
    )
    conn.commit()
    return cur.lastrowid
