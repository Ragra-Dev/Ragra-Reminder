"""Server-side sessions.

The session token is a 256-bit value from `secrets.token_urlsafe`. Only its
SHA-256 hash is ever written to the database, so the stored row is not a
credential: reading `sessions` gives an attacker nothing they can replay.
Lookup is by exact hash, so this costs nothing in speed or complexity.

A plain SHA-256 is the right primitive here, unlike for passwords. The
token is 256 bits of CSPRNG output with no structure to guess, so there is
no dictionary to run and nothing a slow KDF would buy; the property being
bought is one-wayness, not brute-force resistance.

Two expiries are enforced together (see migration 0021): an absolute
ceiling so a session cannot live forever, and an idle timeout so an
abandoned session on a shared machine stops working on its own. A session
is valid only while both hold.

Sessions are always created fresh and never adopted from a client-supplied
value, which is what makes session fixation impossible: an attacker cannot
plant a token and have it become authenticated, because signing in issues a
new token and discards whatever was presented.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from ragra.tz import parse_instant, utc_iso

# The cookie name. Deliberately not "session": a distinct name keeps it from
# colliding with anything else served from localhost during development.
COOKIE_NAME = "ragra_session"

# A signed-in browser stays signed in for a week at most...
ABSOLUTE_LIFETIME = timedelta(days=7)
# ...and at most three days without being used.
IDLE_TIMEOUT = timedelta(days=3)

# 32 bytes of CSPRNG output, URL-safe. Well beyond guessing range, and the
# only place the raw value ever exists outside the user's cookie.
TOKEN_BYTES = 32


class SessionError(RuntimeError):
    """A session operation could not be completed."""


@dataclass(frozen=True)
class Session:
    """A validated session. Carries no token - by the time a caller holds
    one of these, the token has already done its only job."""

    user_id: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


def hash_token(token: str) -> str:
    """The stored form of a session token.

    Separate from `create_session` so the lookup path and the write path
    provably agree, and so tests can assert the raw token never appears in
    the database without reimplementing the hash.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    now: datetime,
    lifetime: timedelta = ABSOLUTE_LIFETIME,
) -> str:
    """Issue a new session for `user_id` and return the raw token.

    The returned token is the only copy that ever exists outside the user's
    browser; it is not stored, logged, or recoverable. Callers set it as a
    cookie and forget it.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now_iso = utc_iso(now)
    conn.execute(
        """INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        (hash_token(token), user_id, now_iso, now_iso, utc_iso(now + lifetime)),
    )
    conn.commit()
    return token


def lookup_session(
    conn: sqlite3.Connection,
    *,
    token: str | None,
    now: datetime,
    idle_timeout: timedelta = IDLE_TIMEOUT,
    touch: bool = True,
) -> Session | None:
    """Resolve a token to a live session, or None.

    ragra:token-scoped - keyed by the token hash rather than by an owner,
    because resolving the owner is what this function does. Its safety
    comes from the token being 256 bits of CSPRNG output, and the row it
    returns names the user_id every later query is scoped to.

    Returns None for every failure mode without distinguishing them -
    absent, unknown, expired, idle-timed-out. The caller has no legitimate
    use for the difference, and reporting it would tell an attacker whether
    a guessed token was ever real.

    An expired row is deleted on the way out rather than left to the purge
    job: the cheapest moment to clean up a dead session is the moment
    something tries to use it.
    """
    if not token:
        return None

    token_hash = hash_token(token)
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None:
        return None

    expires_at = parse_instant(row["expires_at"])
    last_seen_at = parse_instant(row["last_seen_at"])
    if now >= expires_at or now - last_seen_at >= idle_timeout:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None

    if touch:
        # Refreshing the idle clock is what makes an active session stay
        # alive; the absolute ceiling is deliberately not extended, so
        # continuous use cannot turn a session into a permanent one.
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (utc_iso(now), token_hash),
        )
        conn.commit()
        last_seen_at = now

    return Session(
        user_id=row["user_id"],
        created_at=parse_instant(row["created_at"]),
        last_seen_at=last_seen_at,
        expires_at=expires_at,
    )


def revoke_session(conn: sqlite3.Connection, *, token: str | None) -> bool:
    """Sign out one browser.

    ragra:token-scoped - see lookup_session. Deleting by token hash is
    inherently owner-scoped: holding the token is what proves the session
    is yours to end, and an attacker who does not hold it cannot name a row
    to delete.

    Idempotent: revoking an unknown or already revoked token is a
    successful no-op, so a repeated sign-out (or a double-submitted form)
    never errors."""
    if not token:
        return False
    cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
    conn.commit()
    return cur.rowcount > 0


def revoke_all_sessions_for_user(conn: sqlite3.Connection, *, user_id: int) -> int:
    """Sign out every browser for one account. Returns how many were
    revoked.

    This is the lever pulled when something has gone wrong - a suspected
    compromise, a credential change, account deletion - so it deliberately
    takes a user id rather than a token: the person invoking it may not hold
    the sessions being ended.
    """
    cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount


def purge_expired_sessions(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Delete sessions past their absolute expiry.

    ragra:cross-user - retention housekeeping across the whole table, for
    the same reason as repo.purge_old_tick_sessions: an expiry sweep that
    only visited some users would leave dead rows behind forever. It deletes
    strictly by time and returns only a count, so it discloses nothing.
    """
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_iso(now),))
    conn.commit()
    return cur.rowcount


def active_sessions_for_user(conn: sqlite3.Connection, *, user_id: int) -> int:
    """How many live sessions this account holds. Used by the account page
    so the user can see (and end) sign-ins they don't recognise."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,)
    ).fetchone()["c"]
