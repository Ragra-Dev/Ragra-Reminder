-- 0030_telegram_channel
--
-- Telegram as a notification and chat channel. A person is never asked to type
-- an identifier: they press "Connect Telegram", which opens the bot with a
-- one-time code (telegram_link_codes); the bot receives "/start <code>" and
-- learns which Telegram chat belongs to which account. `telegram_chat_id` is
-- therefore only ever written by the server after that handshake - never taken
-- from a form - so nobody can point their alerts at someone else's chat.

ALTER TABLE notification_preferences ADD COLUMN telegram_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_preferences ADD COLUMN telegram_chat_id TEXT;

CREATE TABLE IF NOT EXISTS telegram_link_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
