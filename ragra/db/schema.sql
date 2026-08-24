-- Ragra core schema. SQLite. Applied idempotently (CREATE TABLE IF NOT EXISTS)
-- by ragra/db/connection.py on every startup.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL UNIQUE,      -- Google Classroom course id
    course_code TEXT,                      -- e.g. CS1004, from FAST registration match
    name TEXT NOT NULL,
    section TEXT,
    teacher TEXT,
    state TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE, ARCHIVED, DECLINED (Classroom courseState)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per discovered Classroom item (coursework, announcement, or
-- courseWorkMaterial) that becomes a Ragra task. Deduplicated by the stable
-- external identifier, never by title.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    source_type TEXT NOT NULL,             -- coursework | announcement | material | manual
    external_id TEXT,                      -- Classroom item id; NULL for manual tasks
    title TEXT NOT NULL,
    description TEXT,
    link TEXT,
    kind TEXT NOT NULL DEFAULT 'ACTIONABLE',   -- ACTIONABLE | INFORMATIONAL
    status TEXT NOT NULL DEFAULT 'DISCOVERED', -- DISCOVERED, ACTION_REQUIRED, PLANNED,
                                                -- IN_PROGRESS, COMPLETED, MISSED, CANCELLED, ARCHIVED
    actual_deadline TEXT,                  -- ISO 8601, authoritative (Classroom), NULL if none
    personal_deadline TEXT,                -- ISO 8601, Hashim's intended completion time
    source_published_at TEXT,              -- Classroom creationTime, for informational triage
    source_updated_at TEXT,                -- Classroom updateTime, drives change detection
    completed_at TEXT,
    missed_at TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(course_id, source_type, external_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_actual_deadline ON tasks(actual_deadline);

-- Append-only audit trail. Never deleted or overwritten.
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    changed_at TEXT NOT NULL,
    field TEXT NOT NULL,           -- e.g. actual_deadline, status, title
    old_value TEXT,
    new_value TEXT
);

-- One row per scheduled reminder instance. idempotency_key is derived from
-- (task_id, reminder_type, actual_deadline) so re-running the scheduler, or
-- recomputing after a deadline change, can never produce a duplicate send.
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    reminder_type TEXT NOT NULL,   -- e.g. T_MINUS_3D, T_MINUS_1D, DUE_TODAY, FINAL_1H
    scheduled_for TEXT NOT NULL,   -- ISO 8601
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, SENT, CANCELLED, FAILED (FAILED = permanently
                                             -- failed, retries exhausted - PENDING covers "not yet
                                             -- attempted" and "attempted, retrying" alike)
    sent_at TEXT,
    last_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,  -- bounded retry: see ragra/reminders/dispatch.py
    next_retry_at TEXT,                        -- NULL = eligible now (subject to scheduled_for)
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_dispatch ON reminders(status, scheduled_for);

-- Ragra-owned Google Calendar events. One row per event Ragra created; the
-- google_event_id is reused on every sync so events are updated, not
-- duplicated.
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    timetable_event_id INTEGER,
    kind TEXT NOT NULL,            -- ACTUAL_DEADLINE | PERSONAL_PLAN | CLASS
    google_event_id TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL
);

-- Tracks the health of each recurring sync source, so a failed run never
-- silently stalls and existing data is never dropped because of a
-- transient API failure.
CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,       -- classroom | calendar | timetable
    last_synced_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'NEVER_RUN' -- NEVER_RUN, OK, ERROR
);

-- Self-alerting: one row per pipeline component (classroom, calendar,
-- reminders, tick). A healthy run resets consecutive_failures to 0 and
-- clears last_alert_sent_at, re-arming the alert for a future streak. See
-- ragra/health.py.
CREATE TABLE IF NOT EXISTS pipeline_health (
    component TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT,
    last_alert_sent_at TEXT
);

CREATE TABLE IF NOT EXISTS timetable_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id),
    external_id TEXT NOT NULL UNIQUE,  -- stable id from the FAST timetable source
    day_of_week INTEGER NOT NULL,      -- 0=Monday .. 6=Sunday
    start_time TEXT NOT NULL,          -- HH:MM
    end_time TEXT NOT NULL,            -- HH:MM
    room TEXT,
    instructor TEXT,
    section TEXT,
    status TEXT NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED, CANCELLED, RESCHEDULED
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
