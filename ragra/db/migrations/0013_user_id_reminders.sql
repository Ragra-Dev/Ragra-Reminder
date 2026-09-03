-- 0013_user_id_reminders
-- ragra:foreign-keys-off
--
-- idempotency_key's global UNIQUE becomes UNIQUE(user_id, idempotency_key).
-- The key is built as "<task_id>:<reminder_type>:<deadline>" (see
-- ragra/sync/classroom_sync.py), so today it happens not to collide across
-- users because task ids are globally unique - but that is an accident of the
-- current key format, not a guarantee. Scoping the constraint to the owner
-- makes per-user idempotency a property of the schema rather than of a string
-- format someone might reasonably change later.
--
-- The bounded-retry columns (attempt_count, next_retry_at) and the dispatch
-- index are preserved exactly; reminder delivery semantics are untouched by
-- this migration.

DROP TABLE IF EXISTS reminders_new;

CREATE TABLE reminders_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    reminder_type TEXT NOT NULL,   -- e.g. T_MINUS_3D, T_MINUS_1D, DUE_TODAY, FINAL_1H
    scheduled_for TEXT NOT NULL,   -- ISO 8601
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, SENT, CANCELLED, FAILED
    sent_at TEXT,
    last_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key)
);

INSERT INTO reminders_new
    (id, user_id, task_id, reminder_type, scheduled_for, status, sent_at, last_error,
     attempt_count, next_retry_at, idempotency_key, created_at)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    task_id, reminder_type, scheduled_for, status, sent_at, last_error,
    attempt_count, next_retry_at, idempotency_key, created_at
FROM reminders;

DROP TABLE reminders;
ALTER TABLE reminders_new RENAME TO reminders;

CREATE INDEX IF NOT EXISTS idx_reminders_dispatch ON reminders(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders(user_id);
CREATE INDEX IF NOT EXISTS idx_reminders_task_id ON reminders(task_id);
