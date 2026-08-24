"""SQLite connection + idempotent schema application."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _ensure_reminder_retry_columns(conn: sqlite3.Connection) -> None:
    """Targeted, idempotent column migration for databases created before
    bounded reminder retry existed. `CREATE TABLE IF NOT EXISTS` (schema.sql)
    only helps brand-new databases; existing ones need these columns added
    explicitly. Not a general migration framework - just the two columns
    this feature needs, safe to run on every connect."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(reminders)")}
    if "attempt_count" not in existing:
        conn.execute("ALTER TABLE reminders ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    if "next_retry_at" not in existing:
        conn.execute("ALTER TABLE reminders ADD COLUMN next_retry_at TEXT")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL instead of the default rollback-journal mode: Ragra now has three
    # things touching the same file concurrently in normal use (the 15-min
    # scheduled tick, a long-running dashboard server, and ad hoc CLI runs).
    # WAL allows readers and a writer to proceed without blocking each other
    # the way the default mode can; transactions stay fully safe either way.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _ensure_reminder_retry_columns(conn)
    conn.commit()
    return conn


@contextmanager
def connect_closing(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
