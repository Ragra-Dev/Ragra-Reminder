"""General, numbered migration framework - applied from connect() after
schema.sql and the legacy column fixes have already run (see
ragra/db/connection.py). This is deliberately separate from, and does not
replace, those two: they are proven safe against real data; this framework
exists only for schema changes from this point forward.

Migration files live in ragra/db/migrations/, named `NNNN_description.sql`
(a zero-padded, strictly increasing, unique version number). Each is
applied at most once, ever, and recorded in the schema_migrations table -
so a rerun against a database that already has every migration applied is
a true no-op, and a database missing some migrations only ever has the
missing ones applied, never a full replay.

Append-only: once a migration file has been applied anywhere, it must never
be edited or renumbered - only new, higher-numbered files get added. This
module enforces that a given version's recorded name cannot silently change
underneath an already-applied migration.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ragra.db import repo

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME_PATTERN = re.compile(r"^(?P<version>\d{4,})_(?P<name>[A-Za-z0-9_]+)\.sql$")

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """A migration file is malformed, out of sequence, or failed to apply.
    Never partially applied: the offending migration's failure is raised
    before it is recorded, so a retry (after fixing the cause) will attempt
    it again rather than skip it."""


@dataclass(frozen=True)
class _MigrationFile:
    version: int
    name: str
    path: Path


def _discover_migrations(migrations_dir: Path) -> list[_MigrationFile]:
    discovered: dict[int, _MigrationFile] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _FILENAME_PATTERN.match(path.name)
        if not match:
            raise MigrationError(
                f"migration file {path.name!r} does not match the required "
                f"'NNNN_description.sql' naming convention"
            )
        version = int(match.group("version"))
        if version in discovered:
            raise MigrationError(
                f"duplicate migration version {version}: {discovered[version].path.name!r} "
                f"and {path.name!r}"
            )
        discovered[version] = _MigrationFile(version=version, name=match.group("name"), path=path)
    return [discovered[version] for version in sorted(discovered)]


def apply_pending_migrations(conn: sqlite3.Connection, *, migrations_dir: Path | None = None) -> list[str]:
    """Applies every migration in `migrations_dir` (default:
    ragra/db/migrations/) not yet recorded in schema_migrations, in
    ascending version order, each inside its own transaction. Returns the
    names of migrations actually applied this call (empty if everything was
    already applied - idempotent rerun)."""
    directory = migrations_dir if migrations_dir is not None else _MIGRATIONS_DIR
    conn.execute(_CREATE_MIGRATIONS_TABLE)
    conn.commit()

    applied_versions = {
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    }
    applied_names = {
        row["version"]: row["name"] for row in conn.execute("SELECT version, name FROM schema_migrations")
    }

    newly_applied: list[str] = []
    for migration in _discover_migrations(directory):
        if migration.version in applied_versions:
            recorded_name = applied_names[migration.version]
            if recorded_name != migration.name:
                raise MigrationError(
                    f"migration {migration.version} was applied as {recorded_name!r} but the "
                    f"file on disk is now named {migration.name!r} - migrations must never be "
                    f"renamed or renumbered after being applied"
                )
            continue

        sql_text = migration.path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql_text)
        except sqlite3.Error as exc:
            conn.rollback()
            raise MigrationError(f"migration {migration.path.name} failed to apply: {exc}") from exc

        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, repo.now_iso()),
        )
        conn.commit()
        newly_applied.append(migration.name)

    return newly_applied
