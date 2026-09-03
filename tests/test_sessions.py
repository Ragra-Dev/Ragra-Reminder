"""Session store behaviour and its security properties.

These are not only happy-path tests. The properties that matter here are
negative ones - the token is never stored, an expired session cannot be
used, one user's session never resolves to another - and each is asserted
directly rather than inferred from a successful login.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ragra.web import sessions
from tests.support import make_user, owner_id

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def alice(conn) -> int:
    return owner_id(conn)


@pytest.fixture
def bea(conn) -> int:
    return make_user(conn, google_sub="sessions-second", display_name="Bea")


# ---------------------------------------------------------------------------
# The token is a credential, and is treated like one
# ---------------------------------------------------------------------------


def test_the_raw_token_is_never_written_to_the_database(conn, alice):
    """The whole point of hashing. A backup, a stray copy, or a read of this
    table must yield nothing that can be replayed as a login."""
    token = sessions.create_session(conn, user_id=alice, now=NOW)

    stored = [
        str(value)
        for row in conn.execute("SELECT * FROM sessions")
        for value in tuple(row)
    ]
    assert token not in stored
    assert sessions.hash_token(token) in stored


def test_two_sessions_never_share_a_token(conn, alice, bea):
    tokens = {
        sessions.create_session(conn, user_id=alice, now=NOW),
        sessions.create_session(conn, user_id=alice, now=NOW),
        sessions.create_session(conn, user_id=bea, now=NOW),
    }
    assert len(tokens) == 3


def test_a_token_is_long_enough_to_be_unguessable(conn, alice):
    """A short or structured token would make every other property here
    irrelevant, so the length is asserted rather than assumed from the
    generator's name."""
    token = sessions.create_session(conn, user_id=alice, now=NOW)
    assert len(token) >= 32


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_a_fresh_token_resolves_to_its_owner(conn, alice):
    token = sessions.create_session(conn, user_id=alice, now=NOW)
    session = sessions.lookup_session(conn, token=token, now=NOW)
    assert session is not None
    assert session.user_id == alice


def test_one_users_token_never_resolves_to_another_user(conn, alice, bea):
    alice_token = sessions.create_session(conn, user_id=alice, now=NOW)
    bea_token = sessions.create_session(conn, user_id=bea, now=NOW)

    assert sessions.lookup_session(conn, token=alice_token, now=NOW).user_id == alice
    assert sessions.lookup_session(conn, token=bea_token, now=NOW).user_id == bea


@pytest.mark.parametrize(
    "token", [None, "", "not-a-real-token", "x" * 43], ids=["none", "empty", "garbage", "plausible"]
)
def test_an_invalid_token_resolves_to_nothing(conn, alice, token):
    sessions.create_session(conn, user_id=alice, now=NOW)
    assert sessions.lookup_session(conn, token=token, now=NOW) is None


def test_a_hash_presented_as_a_token_does_not_authenticate(conn, alice):
    """Someone who read the database must not be able to sign in with what
    they found. Presenting the stored hash hashes it again, which matches
    nothing - this is exactly what storing the hash buys."""
    token = sessions.create_session(conn, user_id=alice, now=NOW)
    stored_hash = sessions.hash_token(token)

    assert sessions.lookup_session(conn, token=stored_hash, now=NOW) is None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_a_session_stops_working_at_its_absolute_expiry(conn, alice):
    token = sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(hours=1))

    assert sessions.lookup_session(conn, token=token, now=NOW + timedelta(minutes=59)) is not None
    assert sessions.lookup_session(conn, token=token, now=NOW + timedelta(hours=1)) is None


def test_continuous_use_does_not_extend_the_absolute_ceiling(conn, alice):
    """Refreshing the idle clock must not turn an active session into a
    permanent one."""
    token = sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(hours=2))

    for minutes in (30, 60, 90):
        assert sessions.lookup_session(conn, token=token, now=NOW + timedelta(minutes=minutes))

    assert sessions.lookup_session(conn, token=token, now=NOW + timedelta(hours=2)) is None


def test_an_idle_session_expires_even_well_before_its_ceiling(conn, alice):
    token = sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(days=30))

    stale = NOW + sessions.IDLE_TIMEOUT
    assert sessions.lookup_session(conn, token=token, now=stale) is None


def test_using_a_session_resets_the_idle_clock(conn, alice):
    token = sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(days=30))
    halfway = NOW + sessions.IDLE_TIMEOUT - timedelta(hours=1)

    assert sessions.lookup_session(conn, token=token, now=halfway) is not None
    # Without the refresh this second lookup would be past the idle timeout
    # measured from creation.
    assert sessions.lookup_session(conn, token=token, now=halfway + timedelta(hours=2)) is not None


def test_an_expired_session_is_deleted_on_the_attempt_to_use_it(conn, alice):
    token = sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(hours=1))
    sessions.lookup_session(conn, token=token, now=NOW + timedelta(hours=2))

    remaining = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
    assert remaining == 0


def test_a_read_only_lookup_does_not_refresh_the_idle_clock(conn, alice):
    """`touch=False` exists for callers that inspect a session without it
    counting as activity; if it silently refreshed anyway, idle timeout
    would be unenforceable for them."""
    token = sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(days=30))
    before = conn.execute("SELECT last_seen_at FROM sessions").fetchone()["last_seen_at"]

    sessions.lookup_session(conn, token=token, now=NOW + timedelta(hours=5), touch=False)

    after = conn.execute("SELECT last_seen_at FROM sessions").fetchone()["last_seen_at"]
    assert after == before


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_revoking_a_session_takes_effect_immediately(conn, alice):
    token = sessions.create_session(conn, user_id=alice, now=NOW)

    assert sessions.revoke_session(conn, token=token) is True
    assert sessions.lookup_session(conn, token=token, now=NOW) is None


def test_revoking_is_idempotent(conn, alice):
    token = sessions.create_session(conn, user_id=alice, now=NOW)
    sessions.revoke_session(conn, token=token)

    # A double-submitted sign-out must not error.
    assert sessions.revoke_session(conn, token=token) is False
    assert sessions.revoke_session(conn, token=None) is False


def test_revoking_one_session_leaves_the_users_other_browsers_signed_in(conn, alice):
    phone = sessions.create_session(conn, user_id=alice, now=NOW)
    laptop = sessions.create_session(conn, user_id=alice, now=NOW)

    sessions.revoke_session(conn, token=phone)

    assert sessions.lookup_session(conn, token=laptop, now=NOW) is not None


def test_revoking_all_sessions_signs_out_only_that_user(conn, alice, bea):
    alice_token = sessions.create_session(conn, user_id=alice, now=NOW)
    sessions.create_session(conn, user_id=alice, now=NOW)
    bea_token = sessions.create_session(conn, user_id=bea, now=NOW)

    assert sessions.revoke_all_sessions_for_user(conn, user_id=alice) == 2

    assert sessions.lookup_session(conn, token=alice_token, now=NOW) is None
    assert sessions.lookup_session(conn, token=bea_token, now=NOW) is not None


def test_deleting_a_user_invalidates_their_sessions(conn, alice, bea):
    """The cascade P3-11 relies on: an account that no longer exists must
    not have a browser still holding a working session for it."""
    bea_token = sessions.create_session(conn, user_id=bea, now=NOW)
    alice_token = sessions.create_session(conn, user_id=alice, now=NOW)

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users WHERE id = ?", (bea,))
    conn.commit()

    assert sessions.lookup_session(conn, token=bea_token, now=NOW) is None
    assert sessions.lookup_session(conn, token=alice_token, now=NOW) is not None


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def test_purging_removes_only_expired_sessions(conn, alice, bea):
    expired = sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(hours=1))
    live = sessions.create_session(conn, user_id=bea, now=NOW, lifetime=timedelta(days=7))

    assert sessions.purge_expired_sessions(conn, now=NOW + timedelta(hours=2)) == 1

    assert sessions.lookup_session(conn, token=expired, now=NOW + timedelta(hours=2)) is None
    assert sessions.lookup_session(conn, token=live, now=NOW + timedelta(hours=2)) is not None


def test_purging_is_idempotent(conn, alice):
    sessions.create_session(conn, user_id=alice, now=NOW, lifetime=timedelta(hours=1))
    later = NOW + timedelta(hours=2)

    assert sessions.purge_expired_sessions(conn, now=later) == 1
    assert sessions.purge_expired_sessions(conn, now=later) == 0


def test_active_session_count_is_per_user(conn, alice, bea):
    sessions.create_session(conn, user_id=alice, now=NOW)
    sessions.create_session(conn, user_id=alice, now=NOW)
    sessions.create_session(conn, user_id=bea, now=NOW)

    assert sessions.active_sessions_for_user(conn, user_id=alice) == 2
    assert sessions.active_sessions_for_user(conn, user_id=bea) == 1
