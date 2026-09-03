-- 0015_user_id_timetable_events
-- ragra:foreign-keys-off
--
-- external_id's global UNIQUE becomes UNIQUE(user_id, external_id). This is
-- the most likely of the four uniqueness bugs to actually fire: the timetable
-- external_id is derived from (program, batch year, section, course name,
-- occurrence index) - see ragra/sync/timetable_sync.py - so two classmates in
-- the same section would generate byte-identical keys for the same lecture.
-- Under the old global constraint the second user's timetable sync would fail
-- outright.

DROP TABLE IF EXISTS timetable_events_new;

CREATE TABLE timetable_events_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id),  -- optional link to a matched Classroom course
    external_id TEXT NOT NULL,
    course_name TEXT,                  -- from the user's enrollment config, not raw sheet text
    program TEXT,
    batch_year TEXT,                   -- NULL for REPEAT enrollment
    enrollment_type TEXT NOT NULL DEFAULT 'REGULAR', -- REGULAR | REPEAT
    day_of_week INTEGER NOT NULL,      -- 0=Monday .. 6=Sunday
    occurrence_index INTEGER NOT NULL DEFAULT 0,
    start_time TEXT NOT NULL,          -- HH:MM, 24h, campus wall-clock
    end_time TEXT NOT NULL,            -- HH:MM, 24h, campus wall-clock
    room TEXT,
    instructor TEXT,
    section TEXT,
    status TEXT NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED, CANCELLED, RESCHEDULED
    source_spreadsheet_id TEXT,
    source_sheet_gid TEXT,
    source_sheet_title TEXT,
    last_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, external_id)
);

INSERT INTO timetable_events_new
    (id, user_id, course_id, external_id, course_name, program, batch_year, enrollment_type,
     day_of_week, occurrence_index, start_time, end_time, room, instructor, section, status,
     source_spreadsheet_id, source_sheet_gid, source_sheet_title, last_synced_at,
     created_at, updated_at)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    course_id, external_id, course_name, program, batch_year, enrollment_type,
    day_of_week, occurrence_index, start_time, end_time, room, instructor, section, status,
    source_spreadsheet_id, source_sheet_gid, source_sheet_title, last_synced_at,
    created_at, updated_at
FROM timetable_events;

DROP TABLE timetable_events;
ALTER TABLE timetable_events_new RENAME TO timetable_events;

CREATE INDEX IF NOT EXISTS idx_timetable_events_user_id ON timetable_events(user_id);
