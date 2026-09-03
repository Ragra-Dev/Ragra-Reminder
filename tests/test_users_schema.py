"""Tests for the users table (migration 0009) - the tenant anchor.

Nothing references users yet; this sub-phase only establishes the table and
the single seed row representing the pre-identity owner of all existing
data. The behaviour that matters here is entirely about not breaking that
existing data: the seed must happen exactly once, must survive reconnects,
and must never produce a second owner.
"""

import sqlite3

import pytest

from ragra.db import repo
from ragra.db.connection import connect


def test_users_table_exists_with_expected_columns(conn):
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(users)")}

    assert set(columns) == {"id", "google_sub", "email", "display_name", "created_at", "updated_at"}
    assert columns["created_at"]["notnull"] == 1
    assert columns["updated_at"]["notnull"] == 1
    # google_sub must stay nullable: the seeded pre-identity owner has no
    # Google account linked until first sign-in.
    assert columns["google_sub"]["notnull"] == 0


def test_exactly_one_user_is_seeded_on_a_fresh_database(conn):
    users = repo.list_users(conn)

    assert len(users) == 1
    assert users[0]["google_sub"] is None
    assert users[0]["email"] is None


def test_seed_is_not_duplicated_across_reconnects(tmp_path):
    db_path = tmp_path / "seed.db"
    first = connect(db_path)
    before = len(repo.list_users(first))
    first.close()

    second = connect(db_path)
    after = len(repo.list_users(second))
    second.close()

    assert before == 1
    assert after == 1


def test_seeded_timestamps_use_the_canonical_iso_form(conn):
    user = repo.list_users(conn)[0]

    # Same '...T...+00:00' shape as every other timestamp Ragra writes -
    # SQLite's default 'YYYY-MM-DD HH:MM:SS' would sort differently.
    assert "T" in user["created_at"]
    assert user["created_at"].endswith("+00:00")


def test_google_sub_is_unique_but_permits_multiple_unlinked_users(conn):
    now = repo.now_iso()
    conn.execute(
        "INSERT INTO users (google_sub, email, display_name, created_at, updated_at) "
        "VALUES (NULL, NULL, 'second unlinked', ?, ?)",
        (now, now),
    )
    conn.commit()  # two NULLs must coexist - SQLite treats NULLs as distinct

    conn.execute(
        "INSERT INTO users (google_sub, email, display_name, created_at, updated_at) "
        "VALUES ('sub-123', NULL, 'real', ?, ?)",
        (now, now),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (google_sub, email, display_name, created_at, updated_at) "
            "VALUES ('sub-123', NULL, 'duplicate', ?, ?)",
            (now, now),
        )


def test_lookup_by_google_sub(conn):
    now = repo.now_iso()
    conn.execute(
        "INSERT INTO users (google_sub, email, display_name, created_at, updated_at) "
        "VALUES ('sub-abc', 'someone@example.com', 'Someone', ?, ?)",
        (now, now),
    )
    conn.commit()

    found = repo.get_user_by_google_sub(conn, google_sub="sub-abc")

    assert found is not None
    assert found["email"] == "someone@example.com"
    assert repo.get_user_by_google_sub(conn, google_sub="nope") is None


def test_unlinked_user_id_returns_the_single_pre_identity_owner(conn):
    assert repo.unlinked_user_id(conn) == repo.list_users(conn)[0]["id"]


def test_unlinked_user_id_is_none_when_ambiguous(conn):
    now = repo.now_iso()
    conn.execute(
        "INSERT INTO users (google_sub, email, display_name, created_at, updated_at) "
        "VALUES (NULL, NULL, 'second unlinked', ?, ?)",
        (now, now),
    )
    conn.commit()

    # Two candidates: adoption must refuse rather than pick one arbitrarily.
    assert repo.unlinked_user_id(conn) is None


def test_unlinked_user_id_is_none_once_every_user_is_linked(conn):
    conn.execute("UPDATE users SET google_sub = 'sub-linked'")
    conn.commit()

    assert repo.unlinked_user_id(conn) is None


def test_get_user_returns_none_for_unknown_id(conn):
    assert repo.get_user(conn, user_id=999999) is None
