-- 0025_notification_preferences
--
-- Where each user's reminders are delivered.
--
-- The split matters and is the whole design here: *infrastructure* stays in
-- the environment (which SMTP server to relay through, where the optional
-- Hermes binary lives), while *destination* becomes per-user data (which
-- address, which messaging target). Infrastructure is a property of the
-- deployment and is identical for everyone; a destination is a property of
-- a person, and the failure mode of getting that wrong is not a broken
-- feature - it is one user's academic deadlines being delivered to another
-- user's phone.
--
-- Nothing here is a secret: an address is not a credential. SMTP passwords
-- stay in the environment and are never written to this table, which is why
-- it needs no encryption while google_credentials does.
--
-- A user with no row is not an error. It means "no delivery configured",
-- which is already a fully supported state everywhere in Ragra - reminders
-- stay PENDING rather than being sent nowhere, and nothing is lost.

CREATE TABLE IF NOT EXISTS notification_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    email_enabled INTEGER NOT NULL DEFAULT 0,
    email_to TEXT,                 -- recipient address; not a secret
    hermes_enabled INTEGER NOT NULL DEFAULT 0,
    hermes_target TEXT,            -- Hermes delivery target; not a secret
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
