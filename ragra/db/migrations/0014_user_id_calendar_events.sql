-- 0014_user_id_calendar_events
-- ragra:foreign-keys-off
--
-- google_event_id's global UNIQUE becomes UNIQUE(user_id, google_event_id).
-- Each user authorises their own Google Calendar, so event ids come from
-- different calendars and are only meaningful per owner. A global constraint
-- would also have been a cross-user information leak of a weak kind: one
-- user's insert failing would reveal that another user's calendar already
-- held that event id.
--
-- task_id stays nullable (it always was) so a calendar event can outlive or
-- precede a task link; user_id is NOT NULL regardless, because an event with
-- no owner is unreachable by every per-user query and would leak into none of
-- them - it would simply become invisible, undeletable garbage.

DROP TABLE IF EXISTS calendar_events_new;

CREATE TABLE calendar_events_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id),
    kind TEXT NOT NULL,            -- ACTUAL_DEADLINE | PERSONAL_PLAN
    google_event_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, google_event_id)
);

INSERT INTO calendar_events_new (id, user_id, task_id, kind, google_event_id, updated_at)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    task_id, kind, google_event_id, updated_at
FROM calendar_events;

DROP TABLE calendar_events;
ALTER TABLE calendar_events_new RENAME TO calendar_events;

CREATE INDEX IF NOT EXISTS idx_calendar_events_user_id ON calendar_events(user_id);
CREATE INDEX IF NOT EXISTS idx_calendar_events_task_id ON calendar_events(task_id);
