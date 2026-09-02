-- 0007_personal_course
--
-- Seeds the '__personal__' pseudo-course that holds every manually created
-- task (docs/INTERFACES.md contract #5). Manual tasks reuse the existing
-- tasks table rather than getting their own, so every existing capability -
-- reminders, personal deadlines, history, completion, the dashboard - works
-- on them with no special-casing. The pseudo-course is what lets that reuse
-- happen without making tasks.course_id nullable.
--
-- external_id '__personal__' can never collide with a real Classroom course
-- id (those are numeric strings), and INSERT OR IGNORE makes this safe to
-- re-run.
--
-- Timestamps are written in the same canonical ISO form Ragra uses
-- everywhere ('...T...+00:00'), not SQLite's default 'YYYY-MM-DD HH:MM:SS'.
-- Mixing the two would break the lexicographic ordering the query layer
-- relies on.

INSERT OR IGNORE INTO courses
    (external_id, course_code, name, section, teacher, state, created_at, updated_at)
VALUES (
    '__personal__',
    NULL,
    'Personal',
    NULL,
    NULL,
    'ACTIVE',
    strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
    strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
);
