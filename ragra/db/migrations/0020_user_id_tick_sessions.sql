-- 0020_user_id_tick_sessions
-- ragra:foreign-keys-off
--
-- Final table of the tenant-key migration.
--
-- tick_sessions is short-retention operational diagnostics (auto-purged after
-- ~48 hours), never academic data. Its per-stage result columns are already
-- per-user concepts, so under a multi-user tick the natural shape is one row
-- per user per run rather than one row per run - which is why user_id is
-- NOT NULL here too rather than nullable with a "whole run" sentinel. Rows
-- from a single run are correlated by their shared started_at.
--
-- ON DELETE CASCADE keeps account deletion complete: a deleted user leaves no
-- diagnostic trace behind either.

DROP TABLE IF EXISTS tick_sessions_new;

CREATE TABLE tick_sessions_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds REAL,
    exit_code INTEGER,
    classroom_result TEXT,
    calendar_result TEXT,
    reminders_result TEXT,
    timetable_result TEXT,
    class_reminders_result TEXT,
    error TEXT
);

INSERT INTO tick_sessions_new
    (id, user_id, started_at, finished_at, duration_seconds, exit_code, classroom_result,
     calendar_result, reminders_result, timetable_result, class_reminders_result, error)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    started_at, finished_at, duration_seconds, exit_code, classroom_result,
    calendar_result, reminders_result, timetable_result, class_reminders_result, error
FROM tick_sessions;

DROP TABLE tick_sessions;
ALTER TABLE tick_sessions_new RENAME TO tick_sessions;

CREATE INDEX IF NOT EXISTS idx_tick_sessions_started_at ON tick_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_tick_sessions_user_id ON tick_sessions(user_id);
