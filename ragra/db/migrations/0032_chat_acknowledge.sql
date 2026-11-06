-- 0032_chat_acknowledge
--
-- Alerts in the in-app chat carry a "Received" button. Until it is pressed the
-- alert is repeated on the person's channels every few minutes
-- (ragra/reminders/nags.py): `next_nag_at` is when the next repeat is due and
-- `nag_count` how many have gone out. Pressing the button sets `acked_at`.

ALTER TABLE chat_messages ADD COLUMN acked_at TEXT;
ALTER TABLE chat_messages ADD COLUMN nag_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chat_messages ADD COLUMN next_nag_at TEXT;
