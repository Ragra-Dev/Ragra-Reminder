-- 0026_credential_health
--
-- Durable, per-user, per-service Google credential health. Exists because
-- without it a dead refresh token (Google's own invalid_grant) can go
-- unnoticed indefinitely: the refresh code discarded Google's specific
-- error into a generic string, nothing persisted a classification, and
-- the one UI meant to show connection status (`/account`) only ever
-- queried whether a row existed in `google_credentials` - which says
-- nothing about whether that credential currently works.
--
-- This table is the single, durable answer to "is this connection actually
-- healthy right now", independent of which storage backend (the legacy
-- file path or the encrypted `google_credentials` table) actually holds
-- the credential, and independent of whether anyone has looked at a log
-- file. It is written on every refresh attempt, success or failure, so it
-- survives a process restart and never depends on something being caught
-- "live".
--
-- What each column is for:
--
--   state             - one of: healthy, degraded, reauth_required,
--                        not_connected, config_error. Never inferred from
--                        "did a request just fail" alone - see
--                        ragra/adapters/credential_health.py, which
--                        classifies the *specific* Google/local error
--                        before this is written.
--   last_attempt_at    - every attempt, whether it succeeded or not. Lets a
--                        stuck/never-ticking account be told apart from one
--                        that is failing every time.
--   last_success_at    - the last time this credential actually worked.
--                        Never cleared on failure, so "how stale is my
--                        data" is always answerable even mid-outage.
--   first_failure_at   - when the *current* failure streak began (NULL
--                        while healthy or not_connected). Cleared the
--                        moment a refresh succeeds again.
--   last_error_category - a short, closed, secret-free code (e.g.
--                        "invalid_grant", "transport_error",
--                        "key_missing") - never a raw exception message,
--                        and never anything that could contain a token.
--   last_error_detail  - a short, human-readable, secret-free sentence.
--                        Every write to this column goes through the same
--                        classifier, which never touches token/secret
--                        values - it only ever inspects error *type* and
--                        Google's own `error` field, both of which are
--                        codes, not credentials.
--
-- ON DELETE CASCADE: deleting an account must not leave a credential
-- health row for a user_id that no longer exists.

CREATE TABLE IF NOT EXISTS credential_health (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service TEXT NOT NULL,             -- classroom | calendar
    state TEXT NOT NULL,               -- healthy | degraded | reauth_required | not_connected | config_error
    last_attempt_at TEXT,              -- UTC ISO (+00:00)
    last_success_at TEXT,              -- UTC ISO (+00:00)
    first_failure_at TEXT,             -- UTC ISO (+00:00), NULL unless currently failing
    last_error_category TEXT,          -- short closed code, never a secret
    last_error_detail TEXT,            -- short safe sentence, never a secret
    updated_at TEXT NOT NULL,          -- UTC ISO (+00:00)
    PRIMARY KEY (user_id, service)
);
