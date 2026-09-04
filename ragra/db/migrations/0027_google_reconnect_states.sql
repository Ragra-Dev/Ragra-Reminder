-- 0027_google_reconnect_states
--
-- One row per in-flight "Reconnect Google" attempt - the low-friction
-- recovery action from the account page, distinct from `oauth_states`
-- (migration 0022) on purpose: `oauth_states` is for *signing in to
-- Ragra itself* (identity only, no Google data access, no user_id yet
-- because nobody is authenticated when it starts). This table is for
-- *authorizing Classroom/Calendar data access*, which can only ever be
-- started by a user who is already signed in, for one specific already-
-- known service. Conflating the two would mean a login-flow row and a
-- data-authorization-flow row look identical at the point they're
-- consumed, which is exactly the kind of ambiguity ragra:token-scoped
-- code is supposed to avoid.
--
-- Same safety shape as oauth_states: state_hash rather than the raw
-- value, PKCE verifier, single-use (deleted on consumption), bounded
-- lifetime. ragra:token-scoped applies to lookups here for the same
-- reason it applies to session lookup - resolving which attempt a
-- callback belongs to is what this table is for, so it cannot itself
-- require the user_id it is about to reveal.
--
-- user_id IS stored (unlike oauth_states) because the user starting this
-- is already signed in - the whole point is to attach a new Google
-- authorization to an *existing* account, never to create one.

CREATE TABLE IF NOT EXISTS google_reconnect_states (
    state_hash TEXT PRIMARY KEY,   -- SHA-256 of the state parameter, hex
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service TEXT NOT NULL,         -- classroom | calendar
    code_verifier TEXT NOT NULL,   -- PKCE verifier
    created_at TEXT NOT NULL,      -- UTC ISO (+00:00)
    expires_at TEXT NOT NULL       -- UTC ISO
);

CREATE INDEX IF NOT EXISTS idx_google_reconnect_states_expires_at ON google_reconnect_states(expires_at);
