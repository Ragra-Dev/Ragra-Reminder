-- 0034_feedback
--
-- Feedback and suggestions sent from the Contact us page. One row per message;
-- `kind` is what the person picked (suggestion, bug, praise, question) and
-- `rating` is the optional 1-5 stars. Deleted together with the account.

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    rating INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, created_at);
