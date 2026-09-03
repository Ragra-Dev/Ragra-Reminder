-- 0019_user_id_pipeline_health
-- ragra:foreign-keys-off
--
-- PRIMARY KEY(component) becomes PRIMARY KEY(user_id, component), for the
-- same reason as 0018 and with a sharper consequence: pipeline_health drives
-- the self-alert in ragra/health.py, which fires after three consecutive
-- failures of a component. Shared across users, one user's healthy run would
-- reset another user's failure streak - permanently suppressing the alert for
-- a genuinely broken account. That is a silent failure of the exact mechanism
-- meant to catch silent failures.

DROP TABLE IF EXISTS pipeline_health_new;

CREATE TABLE pipeline_health_new (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    component TEXT NOT NULL,       -- classroom | calendar | reminders | timetable | class_reminders
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT,
    last_alert_sent_at TEXT,
    PRIMARY KEY (user_id, component)
);

INSERT INTO pipeline_health_new
    (user_id, component, consecutive_failures, last_success_at, last_failure_at, last_error,
     last_alert_sent_at)
SELECT
    (SELECT id FROM users ORDER BY id LIMIT 1),
    component, consecutive_failures, last_success_at, last_failure_at, last_error,
    last_alert_sent_at
FROM pipeline_health;

DROP TABLE pipeline_health;
ALTER TABLE pipeline_health_new RENAME TO pipeline_health;
