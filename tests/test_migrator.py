"""Tests for ragra.db.migrator - the general, numbered migration framework.
Uses temporary, throwaway migration directories (never the real
ragra/db/migrations/) so these tests can exercise failure/ordering/rename
cases without touching the actual migration history.
"""

from pathlib import Path

import pytest

from ragra.db.connection import connect
from ragra.db.migrator import MigrationError, apply_pending_migrations


def _write(directory: Path, filename: str, sql: str) -> None:
    (directory / filename).write_text(sql, encoding="utf-8")


def test_baseline_migration_is_applied_on_a_fresh_database(conn):
    applied = {row["version"]: row["name"] for row in conn.execute("SELECT version, name FROM schema_migrations")}
    assert applied[1] == "baseline"


def test_every_real_migration_is_applied_on_a_fresh_database(conn):
    # Deliberately not a hardcoded count - this must keep passing as
    # migrations are added, while still proving none were skipped.
    on_disk = {int(p.name.split("_")[0]) for p in (Path("ragra/db/migrations")).glob("*.sql")}
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    assert on_disk == applied


def test_rerun_against_an_already_migrated_database_is_a_no_op(tmp_path):
    db_path = tmp_path / "test.db"
    conn1 = connect(db_path)
    before = conn1.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
    conn1.close()

    conn2 = connect(db_path)
    after = conn2.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
    assert after == before  # reconnecting never re-applies or duplicates
    conn2.close()


def test_new_migration_applies_exactly_once_and_is_idempotent_on_rerun(conn, tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_baseline.sql", "-- baseline\n")
    _write(migrations_dir, "9002_add_note_column.sql", "ALTER TABLE courses ADD COLUMN note TEXT;")

    first = apply_pending_migrations(conn, migrations_dir=migrations_dir)
    assert first == ["add_note_column"]
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(courses)")}
    assert "note" in columns

    second = apply_pending_migrations(conn, migrations_dir=migrations_dir)
    assert second == []  # already applied - true no-op, not a re-run of the ALTER


def test_migrations_apply_in_ascending_version_order(conn, tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_baseline.sql", "-- baseline\n")
    _write(migrations_dir, "9003_third.sql", "ALTER TABLE courses ADD COLUMN third_col TEXT;")
    _write(migrations_dir, "9002_second.sql", "ALTER TABLE courses ADD COLUMN second_col TEXT;")

    applied = apply_pending_migrations(conn, migrations_dir=migrations_dir)
    assert applied == ["second", "third"]


def test_malformed_filename_is_rejected(conn, tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "not_a_migration.sql", "SELECT 1;")

    with pytest.raises(MigrationError):
        apply_pending_migrations(conn, migrations_dir=migrations_dir)


def test_duplicate_version_number_is_rejected(conn, tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_baseline.sql", "-- baseline\n")
    _write(migrations_dir, "9002_first.sql", "SELECT 1;")
    _write(migrations_dir, "9002_second.sql", "SELECT 1;")

    with pytest.raises(MigrationError):
        apply_pending_migrations(conn, migrations_dir=migrations_dir)


def test_renaming_an_already_applied_migration_is_rejected(conn, tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_baseline.sql", "-- baseline\n")
    _write(migrations_dir, "9002_original_name.sql", "ALTER TABLE courses ADD COLUMN renamed_test TEXT;")
    apply_pending_migrations(conn, migrations_dir=migrations_dir)

    (migrations_dir / "9002_original_name.sql").unlink()
    _write(migrations_dir, "9002_different_name.sql", "ALTER TABLE courses ADD COLUMN renamed_test TEXT;")

    with pytest.raises(MigrationError):
        apply_pending_migrations(conn, migrations_dir=migrations_dir)


def test_failed_migration_is_never_recorded_as_applied(conn, tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_baseline.sql", "-- baseline\n")
    _write(migrations_dir, "9002_broken.sql", "ALTER TABLE this_table_does_not_exist ADD COLUMN x TEXT;")

    with pytest.raises(MigrationError):
        apply_pending_migrations(conn, migrations_dir=migrations_dir)

    count = conn.execute(
        "SELECT COUNT(*) AS c FROM schema_migrations WHERE name = 'broken'"
    ).fetchone()["c"]
    assert count == 0


def test_real_migrations_directory_applies_cleanly(tmp_path):
    # Exercises the actual ragra/db/migrations/ directory through the
    # production path: connect() applies schema.sql first, which later
    # migrations legitimately depend on.
    conn = connect(tmp_path / "real-migrations.db")
    applied_again = apply_pending_migrations(conn)
    assert applied_again == []  # connect() already applied them all
    conn.close()
