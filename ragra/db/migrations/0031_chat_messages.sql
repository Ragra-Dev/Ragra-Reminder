-- 0031_chat_messages
--
-- The in-app chat: one thread per account. `role` is who said it - the person
-- ('user'), Ragra answering ('bot'), or Ragra pushing a reminder or
-- announcement ('alert') - so alerts and conversation read as a single
-- WhatsApp-style thread.

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'bot', 'alert')),
    text TEXT NOT NULL,
    link_path TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_user ON chat_messages (user_id, id);
