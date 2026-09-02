-- 0004_tick_class_reminders_result
--
-- The tick gained a fifth stage (class reminders). tick_sessions records
-- one result column per stage, so it needs one more.
--
-- Folding this into reminders_result was the alternative and was rejected:
-- class reminders are tracked as their own pipeline_health component, and
-- a diagnostic that merges two independently-failing stages into one field
-- is exactly the kind of thing that makes an incident harder to read.
--
-- tick_sessions is short-retention operational diagnostics (auto-purged
-- after ~48 hours), never application data, so this column carries no
-- academic meaning.

ALTER TABLE tick_sessions ADD COLUMN class_reminders_result TEXT;
