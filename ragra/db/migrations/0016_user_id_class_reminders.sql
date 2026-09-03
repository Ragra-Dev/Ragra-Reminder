-- 0016_user_id_class_reminders
-- ragra:foreign-keys-off
--
-- idempotency_key's global UNIQUE becomes UNIQUE(user_id, idempotency_key),
-- for the same reason as 0015: the key is
-- "<timetable_event_id>:<occurrence_date>:<reminder_type>", and once two
-- users can hold equivalent timetable rows the "one announcement per class
-- occurrence" guarantee must be per-user rather than global. Without this,
-- one user receiving their 8:30 class reminder would suppress another user's.

DROP TABLE IF EXISTS class_reminders_new;

CREATE TABLE class_reminders_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    timetable_event_id INTEGER NOT NULL REFERENCES timetable_events(id),
    occurrence_date TEXT NOT NULL,          -- campus-local date, YYYY-MM-DD
    reminder_type TEXT NOT NULL,            -- CLASS_SOON
    scheduled_for TEXT NOT NULL,            -- UTC ISO (+00:00): the class start instant
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, SENT, FAILED
    sent_at TEXT,
    last_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key)
);

INSERT INTO class_reminders_new
    (id, user_id, timetable_event_id, occurrence_date, reminder_type, scheduled_for, status,
     sent_at, last_error, attempt_count, idempotency_key, created_at)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    timetable_event_id, occurrence_date, reminder_type, scheduled_for, status,
    sent_at, last_error, attempt_count, idempotency_key, created_at
FROM class_reminders;

DROP TABLE class_reminders;
ALTER TABLE class_reminders_new RENAME TO class_reminders;

CREATE INDEX IF NOT EXISTS idx_class_reminders_dispatch ON class_reminders(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_class_reminders_user_id ON class_reminders(user_id);
