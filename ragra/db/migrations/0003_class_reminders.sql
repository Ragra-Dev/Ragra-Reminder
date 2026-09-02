-- 0003_class_reminders
--
-- Scheduling state for class-start reminders.
--
-- A separate table rather than a nullable reminders.task_id: a class
-- reminder has no task, and SQLite cannot relax a NOT NULL constraint
-- without rebuilding the whole table (create/copy/drop/rename) - a
-- destructive operation against real reminder rows, to gain nothing this
-- table does not already provide. Only the *scheduling state* is separate;
-- delivery still goes through the same provider-neutral notification layer
-- (ragra/adapters/notify.py), so nothing about how a message is actually
-- sent is duplicated.
--
-- Identity is (timetable_event_id, occurrence_date, reminder_type), carried
-- in idempotency_key. Occurrences themselves are never stored - they are
-- recomputed on demand from the weekly pattern (see
-- ragra/timetable/schedule.py), so a timetable edit or a timezone-database
-- update can never leave a stale future instant behind. Only the fact that
-- a reminder was already sent is persistent, which is exactly the piece
-- that must survive a restart.

CREATE TABLE IF NOT EXISTS class_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timetable_event_id INTEGER NOT NULL REFERENCES timetable_events(id),
    occurrence_date TEXT NOT NULL,          -- campus-local date, YYYY-MM-DD
    reminder_type TEXT NOT NULL,            -- CLASS_SOON
    scheduled_for TEXT NOT NULL,            -- UTC ISO (+00:00): the class start instant
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, SENT, FAILED
    sent_at TEXT,
    last_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_class_reminders_dispatch
    ON class_reminders(status, scheduled_for);
