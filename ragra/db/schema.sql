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
    personal_deadline TEXT,                -- ISO 8601, the user's intended completion time
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
-- Note: this is the baseline snapshot. Later schema changes live only in
-- ragra/db/migrations/ - e.g. migration 0006 drops the dead
-- timetable_event_id column this table was originally created with.
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    timetable_event_id INTEGER,
    kind TEXT NOT NULL,            -- ACTUAL_DEADLINE | PERSONAL_PLAN
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

-- Structured, short-retention operational diagnostics for `ragra tick` -
-- deliberately separate from application data (courses/tasks/reminders/
-- timetable_events), which is never expired. Rows older than ~48 hours are
-- purged automatically at the start of every tick (see ragra/cli.py) so
-- this never grows without bound; it exists purely to let a human diagnose
-- a recent problem, not as a permanent audit trail.
CREATE TABLE IF NOT EXISTS tick_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds REAL,
    exit_code INTEGER,
    classroom_result TEXT,
    calendar_result TEXT,
    reminders_result TEXT,
    timetable_result TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_tick_sessions_started_at ON tick_sessions(started_at);

-- One row per weekly class meeting derived from the FAST timetable source,
-- matched against the user's own enrollment config (see
-- ragra/timetable/enrollment.py) - never against sheet color/formatting.
-- external_id is a deterministic key derived from
-- (program, batch_year-or-REPEAT, section, course_name, occurrence_index),
-- not from sheet row/column position, so a reordered sheet updates the
-- existing row instead of duplicating it. occurrence_index disambiguates a
-- course+section that meets more than once a week.
CREATE TABLE IF NOT EXISTS timetable_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id),  -- optional link to a matched Classroom course; not required
    external_id TEXT NOT NULL UNIQUE,
    course_name TEXT,                  -- from the user's enrollment config, not raw sheet text
    program TEXT,
    batch_year TEXT,                   -- NULL for REPEAT enrollment
    enrollment_type TEXT NOT NULL DEFAULT 'REGULAR', -- REGULAR | REPEAT
    day_of_week INTEGER NOT NULL,      -- 0=Monday .. 6=Sunday
    occurrence_index INTEGER NOT NULL DEFAULT 0,
    start_time TEXT NOT NULL,          -- HH:MM, 24h
    end_time TEXT NOT NULL,            -- HH:MM, 24h
    room TEXT,
    instructor TEXT,
    section TEXT,
    status TEXT NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED, CANCELLED, RESCHEDULED
    source_spreadsheet_id TEXT,
    source_sheet_gid TEXT,             -- stable per-tab id, survives tab renames
    source_sheet_title TEXT,           -- human-readable only, never relied on for identity
    last_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
