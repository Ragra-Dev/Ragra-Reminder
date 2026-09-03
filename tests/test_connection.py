"""Regression tests for the connection layer itself: WAL mode and the
targeted reminder-retry column migration for pre-existing databases."""

import sqlite3

from ragra.db.connection import connect


def test_database_opens_in_wal_mode(tmp_path):
    conn = connect(tmp_path / "wal-test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_wal_mode_persists_across_reconnect(tmp_path):
    db_path = tmp_path / "wal-persist-test.db"
    conn1 = connect(db_path)
    conn1.close()

    conn2 = connect(db_path)
    mode = conn2.execute("PRAGMA journal_mode").fetchone()[0]
    conn2.close()
    assert mode.lower() == "wal"


def test_connect_adds_retry_columns_to_a_pre_existing_reminders_table(tmp_path):
    """Simulates a database created before bounded retry existed (no
    attempt_count/next_retry_at columns) - connect() must add them without
    losing any existing data."""
    db_path = tmp_path / "legacy-schema-test.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        """CREATE TABLE courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT NOT NULL UNIQUE,
            course_code TEXT, name TEXT NOT NULL, section TEXT, teacher TEXT,
            state TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    raw.execute(
        """CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id), source_type TEXT,
            external_id TEXT, title TEXT NOT NULL, description TEXT, link TEXT,
            kind TEXT, status TEXT, actual_deadline TEXT, personal_deadline TEXT,
            source_published_at TEXT, source_updated_at TEXT, completed_at TEXT,
            missed_at TEXT, cancelled_at TEXT, created_at TEXT, updated_at TEXT
        )"""
    )
    raw.execute(
        """CREATE TABLE reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id), reminder_type TEXT,
            scheduled_for TEXT, status TEXT DEFAULT 'PENDING', sent_at TEXT,
            last_error TEXT, idempotency_key TEXT UNIQUE, created_at TEXT
        )"""
    )
    # A real parent row, because reminders.task_id has always been a declared
    # foreign key (see schema.sql). Pointing the legacy reminder at a
    # nonexistent task would make this test assert that Ragra tolerates a
    # corrupt database, which is not what it is here to establish.
    raw.execute(
        "INSERT INTO courses (id, external_id, name, created_at, updated_at) "
        "VALUES (1, 'legacy-course', 'Legacy course', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO tasks (id, course_id, source_type, kind, status, title, created_at, updated_at) "
        "VALUES (1, 1, 'coursework', 'ACTIONABLE', 'ACTION_REQUIRED', 'Legacy task', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO reminders (task_id, reminder_type, scheduled_for, idempotency_key, created_at) "
        "VALUES (1, 'T_MINUS_1D', '2026-01-01T00:00:00+00:00', 'legacy-key', '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    conn = connect(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reminders)")}
    assert "attempt_count" in columns
    assert "next_retry_at" in columns

    row = conn.execute("SELECT * FROM reminders WHERE idempotency_key = 'legacy-key'").fetchone()
    assert row is not None
    assert row["attempt_count"] == 0
    assert row["next_retry_at"] is None
    conn.close()


def test_connect_is_idempotent_when_retry_columns_already_exist(tmp_path):
    db_path = tmp_path / "idempotent-migration-test.db"
    connect(db_path).close()
    # Second connect() must not error (e.g. "duplicate column") now that
    # the columns already exist.
    conn = connect(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reminders)")}
    assert "attempt_count" in columns
    conn.close()
