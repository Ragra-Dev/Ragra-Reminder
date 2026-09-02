-- 0005_notification_deliveries
--
-- One row per provider per delivery attempt, so "was I actually told?" has
-- an answer that does not depend on reading logs.
--
-- reminder_id is nullable on purpose: health self-alerts and class
-- reminders are genuine notifications with no row in `reminders`. Recording
-- only task reminders would leave exactly the alerts most worth auditing
-- (the "something is broken" ones) invisible.
--
-- `error` stores whatever the provider reported, which has already passed
-- through the provider's own redaction (see ragra/adapters/notify.py's
-- _redact). Nothing here may ever contain a credential: this table is
-- rendered in the dashboard, so it is a disclosure surface, not just a log.

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER,            -- NULL for health alerts and class reminders
    category TEXT,                  -- Notification.category, e.g. T_MINUS_1D, CLASS_SOON
    provider TEXT NOT NULL,         -- provider class name, never its configuration
    ok INTEGER NOT NULL,            -- 1 = delivered, 0 = failed
    error TEXT,                     -- redacted provider error, NULL on success
    attempted_at TEXT NOT NULL      -- UTC ISO (+00:00)
);

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_attempted_at
    ON notification_deliveries(attempted_at);
