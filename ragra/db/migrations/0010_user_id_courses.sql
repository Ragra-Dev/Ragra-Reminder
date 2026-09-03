-- 0010_user_id_courses
-- ragra:foreign-keys-off
--
-- First table of the tenant-key migration. Rebuilt rather than altered
-- because two separate changes are impossible in place in SQLite:
--
--   1. user_id must be NOT NULL. ALTER TABLE ADD COLUMN cannot add a NOT NULL
--      column without a constant default, and defaulting an owner is exactly
--      the silent mis-attribution this phase exists to prevent.
--   2. external_id's global UNIQUE must become UNIQUE(user_id, external_id).
--      A column-level UNIQUE creates an implicit index that SQLite refuses to
--      drop, so the constraint can only be changed by rebuilding the table.
--
-- (2) is a correctness bug, not a cosmetic one: two users legitimately
-- enrolled in the same Classroom course share an external_id, and the old
-- global constraint would reject the second user's row outright.
--
-- id values are copied verbatim so every child foreign key (tasks.course_id,
-- timetable_events.course_id) stays valid. ON DELETE CASCADE is declared now
-- so account deletion is structurally complete later rather than depending on
-- a hand-maintained list of tables to clear.

DROP TABLE IF EXISTS courses_new;

CREATE TABLE courses_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,             -- Google Classroom course id
    course_code TEXT,                      -- e.g. CS1004, from FAST registration match
    name TEXT NOT NULL,
    section TEXT,
    teacher TEXT,
    state TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE, ARCHIVED, DECLINED (Classroom courseState)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, external_id)
);

INSERT INTO courses_new
    (id, user_id, external_id, course_code, name, section, teacher, state, created_at, updated_at)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    external_id, course_code, name, section, teacher, state, created_at, updated_at
FROM courses;

DROP TABLE courses;
ALTER TABLE courses_new RENAME TO courses;

CREATE INDEX IF NOT EXISTS idx_courses_user_id ON courses(user_id);
