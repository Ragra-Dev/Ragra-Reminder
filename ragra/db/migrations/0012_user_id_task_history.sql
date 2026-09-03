-- 0012_user_id_task_history
-- ragra:foreign-keys-off
--
-- Rebuilt solely to make user_id NOT NULL; no uniqueness constraint changes.
-- task_history is append-only audit data (never updated, never deleted), so
-- carrying a direct owner matters for the same reason it does on tasks: an
-- audit query that forgets to join through tasks would otherwise expose every
-- user's change history.

DROP TABLE IF EXISTS task_history_new;

CREATE TABLE task_history_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    changed_at TEXT NOT NULL,
    field TEXT NOT NULL,           -- e.g. actual_deadline, status, title
    old_value TEXT,
    new_value TEXT
);

INSERT INTO task_history_new (id, user_id, task_id, changed_at, field, old_value, new_value)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    task_id, changed_at, field, old_value, new_value
FROM task_history;

DROP TABLE task_history;
ALTER TABLE task_history_new RENAME TO task_history;

CREATE INDEX IF NOT EXISTS idx_task_history_user_id ON task_history(user_id);
CREATE INDEX IF NOT EXISTS idx_task_history_task_id ON task_history(task_id);
