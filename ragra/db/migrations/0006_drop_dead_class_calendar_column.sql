-- 0006_drop_dead_class_calendar_column
--
-- Removes calendar_events.timetable_event_id, which nothing has ever read
-- or written. It was added for a "push class occurrences to Google
-- Calendar" path that was never built, alongside a kind='CLASS' value that
-- was never used.
--
-- Deleted rather than filled in, deliberately. Class occurrences are not
-- materialised at all under the Phase 2 occurrence model (they are computed
-- on demand - see ragra/timetable/schedule.py), and mirroring them into
-- Google Calendar is not a Phase 2 goal. Keeping a column that implies a
-- link Ragra never makes is schema that lies about what the system does.
--
-- Verified before writing this migration, against a copy of the real
-- database: 28 calendar_events rows, 0 with timetable_event_id set, and
-- 'ACTUAL_DEADLINE' the only kind present. This drop therefore removes no
-- data. The `kind` column itself stays - ACTUAL_DEADLINE is in active use
-- and PERSONAL_PLAN remains a real planned value.

ALTER TABLE calendar_events DROP COLUMN timetable_event_id;
