-- 0023_google_credentials
--
-- Per-user Google authorization, encrypted at rest (see ragra/crypto.py).
--
-- Before this, Ragra's Classroom and Calendar tokens lived in files on
-- disk, which was correct for one user and cannot work for several: a file
-- path has no owner, so a second account would either share the first
-- one's Google access or need a parallel set of paths invented for it.
-- Storing the authorization against the user id makes ownership the same
-- fact everywhere else in the schema already uses.
--
-- `ciphertext` is a BLOB, never text: it is a version byte, a nonce, and
-- AES-256-GCM output. It is bound by associated data to the (user_id,
-- service) it is stored under, so a row moved between users fails to
-- decrypt rather than granting the second user the first one's access.
--
-- `scopes` is stored in the clear on purpose. It is not a secret, and being
-- able to answer "what did this user actually grant?" without decrypting
-- anything is what lets a status command be useful without touching the
-- key.
--
-- ON DELETE CASCADE is doing real work here: deleting an account must
-- destroy its Google authorization along with everything else, or a
-- deleted user would leave a live grant behind (P3-11).

CREATE TABLE IF NOT EXISTS google_credentials (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service TEXT NOT NULL,         -- classroom | calendar
    ciphertext BLOB NOT NULL,      -- version || nonce || AES-256-GCM(sealed)
    scopes TEXT NOT NULL,          -- space-separated, informational, not secret
    created_at TEXT NOT NULL,      -- UTC ISO (+00:00)
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, service)
);
