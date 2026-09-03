-- 0017_user_id_notification_deliveries
-- ragra:foreign-keys-off
--
-- Rebuilt only to make user_id NOT NULL. This table is rendered in the
-- dashboard (the /deliveries page), so it is a disclosure surface as well as
-- a log: without a direct owner, one user's delivery history would appear on
-- another user's page. reminder_id stays nullable because health alerts and
-- class reminders legitimately have no row in `reminders`, which is exactly
-- why ownership cannot be derived through it.

DROP TABLE IF EXISTS notification_deliveries_new;

CREATE TABLE notification_deliveries_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reminder_id INTEGER,            -- NULL for health alerts and class reminders
    category TEXT,                  -- Notification.category, e.g. T_MINUS_1D, CLASS_SOON
    provider TEXT NOT NULL,         -- provider class name, never its configuration
    ok INTEGER NOT NULL,            -- 1 = delivered, 0 = failed
    error TEXT,                     -- redacted provider error, NULL on success
    attempted_at TEXT NOT NULL      -- UTC ISO (+00:00)
);

INSERT INTO notification_deliveries_new
    (id, user_id, reminder_id, category, provider, ok, error, attempted_at)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    reminder_id, category, provider, ok, error, attempted_at
FROM notification_deliveries;

DROP TABLE notification_deliveries;
ALTER TABLE notification_deliveries_new RENAME TO notification_deliveries;

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_attempted_at
    ON notification_deliveries(attempted_at);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_user_id
    ON notification_deliveries(user_id);
