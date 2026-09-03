-- 0018_user_id_sync_state
-- ragra:foreign-keys-off
--
-- PRIMARY KEY(source) becomes PRIMARY KEY(user_id, source). A primary key can
-- only be changed by rebuilding the table.
--
-- This one is a behavioural correctness fix, not just isolation: sync_state
-- records "when did the classroom/calendar/timetable sync last succeed or
-- fail". With a single row per source, the first user's successful sync would
-- overwrite the second user's failure (and vice versa), so a multi-user tick
-- would report one user's health as if it were everyone's - and a genuinely
-- broken account would look healthy because someone else's sync had just
-- succeeded.

DROP TABLE IF EXISTS sync_state_new;

CREATE TABLE sync_state_new (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,          -- classroom | calendar | timetable
    last_synced_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'NEVER_RUN', -- NEVER_RUN, OK, ERROR
    PRIMARY KEY (user_id, source)
);

INSERT INTO sync_state_new (user_id, source, last_synced_at, last_success_at, last_error, status)
SELECT
    (SELECT id FROM users ORDER BY id LIMIT 1),
    source, last_synced_at, last_success_at, last_error, status
FROM sync_state;

DROP TABLE sync_state;
ALTER TABLE sync_state_new RENAME TO sync_state;
