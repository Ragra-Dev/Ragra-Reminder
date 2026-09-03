from pathlib import Path

import pytest

from ragra.db import repo
from ragra.db.connection import connect


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def user_id(conn) -> int:
    """The owner every single-user test acts as.

    Migration 0009 seeds exactly one user, so this is the same row the
    migrated production database's existing data belongs to - tests exercise
    the real ownership path rather than a special test-only user.
    """
    return repo.unlinked_user_id(conn)


@pytest.fixture
def other_user_id(conn) -> int:
    """A second, fully independent account.

    Isolation tests need a neighbour that genuinely exists (rather than an
    unused id), because the interesting failures are the ones where a query
    returns somebody else's real row - not where it returns nothing.
    """
    now = repo.now_iso()
    cur = conn.execute(
        """INSERT INTO users (google_sub, email, display_name, created_at, updated_at)
           VALUES ('test-google-sub-2', NULL, 'Second user', ?, ?)""",
        (now, now),
    )
    conn.commit()
    return cur.lastrowid
