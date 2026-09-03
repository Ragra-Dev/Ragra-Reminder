-- 0009_users
--
-- The tenant anchor table. Every user-owned table gains a user_id
-- referencing this one in migration 0010 onward; nothing references it yet.
--
-- google_sub is Google's stable subject identifier from the ID token, and is
-- the natural key for sign-in. It is deliberately NULLable: this database
-- already contains a full single-user history that predates any concept of
-- identity, so the seed row below represents that existing owner before any
-- Google account has been linked to it. SQLite permits multiple NULLs in a
-- UNIQUE column, which is exactly the behaviour wanted here - unlinked rows
-- never collide, while two accounts can never share a real subject id.
--
-- email is informational only and must never be used as an identity key: a
-- Google account's email can change while its subject id cannot.
--
-- The seed row is what makes the existing data migration in 0010 possible
-- without data loss - every pre-existing row is backfilled to this user.
-- Seeding is conditional on the table being empty rather than on a fixed id,
-- so re-running this migration on any database is a true no-op.
--
-- Timestamps use the same canonical ISO form (...T...+00:00) as every other
-- timestamp Ragra writes; SQLite's default 'YYYY-MM-DD HH:MM:SS' would break
-- the lexicographic ordering the query layer depends on.

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub TEXT UNIQUE,          -- Google ID-token 'sub'; NULL until first sign-in
    email TEXT,                      -- informational only, never an identity key
    display_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO users (google_sub, email, display_name, created_at, updated_at)
SELECT
    NULL,
    NULL,
    'Primary user',
    strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
    strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
WHERE NOT EXISTS (SELECT 1 FROM users);
