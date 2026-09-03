-- 0022_oauth_states
--
-- One row per in-flight sign-in attempt. Deliberately has no user_id: it
-- exists entirely before anyone is authenticated, so there is no owner to
-- attribute it to. That is also why it holds nothing about a person - only
-- the two values needed to finish the round trip safely.
--
-- What each column is for:
--
--   state_hash    - SHA-256 of the OAuth `state` parameter. The callback
--                   only proceeds if the state it was handed matches a row
--                   here, which is what makes a forged callback useless:
--                   an attacker cannot cause a victim's browser to complete
--                   a sign-in the victim's browser did not start. Hashed
--                   for the same reason session tokens are - a read of this
--                   table must not yield anything replayable.
--
--   code_verifier - the PKCE verifier (RFC 7636). Its challenge goes to
--                   Google with the authorization request; the verifier
--                   itself never leaves the server until the token
--                   exchange, so an intercepted authorization code cannot
--                   be redeemed by whoever intercepted it.
--
--   redirect_to   - where to send the user after a successful sign-in.
--                   Validated as a local path before it is stored, never
--                   used raw, so this cannot become an open redirect.
--
-- Rows are single-use: consuming one deletes it, so a captured callback URL
-- cannot be replayed. expires_at bounds how long an abandoned attempt
-- lingers.

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,   -- SHA-256 of the state parameter, hex
    code_verifier TEXT NOT NULL,   -- PKCE verifier
    redirect_to TEXT,              -- validated local path, or NULL for "/"
    created_at TEXT NOT NULL,      -- UTC ISO (+00:00)
    expires_at TEXT NOT NULL       -- UTC ISO
);

CREATE INDEX IF NOT EXISTS idx_oauth_states_expires_at ON oauth_states(expires_at);
