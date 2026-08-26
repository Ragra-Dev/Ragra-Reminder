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


def _ensure_timetable_columns(conn: sqlite3.Connection) -> None:
    """Targeted, idempotent column migration for databases created before
    the FAST timetable columns existed. Same rationale as
    _ensure_reminder_retry_columns: CREATE TABLE IF NOT EXISTS only helps
    brand-new databases."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(timetable_events)")}
    additions = {
        "course_name": "TEXT",
        "program": "TEXT",
        "batch_year": "TEXT",
        "enrollment_type": "TEXT NOT NULL DEFAULT 'REGULAR'",
        "occurrence_index": "INTEGER NOT NULL DEFAULT 0",
        "source_spreadsheet_id": "TEXT",
        "source_sheet_gid": "TEXT",
        "source_sheet_title": "TEXT",
        "last_synced_at": "TEXT",
    }
    for column, ddl_type in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE timetable_events ADD COLUMN {column} {ddl_type}")


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
    _ensure_timetable_columns(conn)
    conn.commit()
    return conn


@contextmanager
def connect_closing(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
