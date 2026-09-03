-- 0021_sessions
--
-- Server-side session store. Introduced by P3-4, ahead of sign-in itself
-- (P3-5), so identity has somewhere to live before anything issues one.
--
-- What is stored is a SHA-256 hash of the session token, never the token.
-- The token exists only in the user's cookie: a read of this table - a
-- backup, a stray copy, an SQL injection - therefore yields nothing that
-- can be replayed as a login. That is the same reason password hashes are
-- stored rather than passwords, and it costs nothing here because lookup is
-- by exact hash, not by comparison.
--
-- Two independent expiries, because they answer different questions:
--   expires_at    - an absolute ceiling, so a session cannot live forever
--                   no matter how actively it is used.
--   last_seen_at  - drives idle timeout, so an abandoned session on a
--                   shared machine stops working without the user having
--                   to remember to sign out.
--
-- ON DELETE CASCADE keeps account deletion (P3-11) complete: deleting a
-- user immediately invalidates every session they hold.

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,   -- SHA-256 of the session token, hex
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,      -- UTC ISO (+00:00)
    last_seen_at TEXT NOT NULL,    -- UTC ISO, refreshed on use; drives idle timeout
    expires_at TEXT NOT NULL       -- UTC ISO, absolute ceiling
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
