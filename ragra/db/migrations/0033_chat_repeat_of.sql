-- 0033_chat_repeat_of
--
-- A repeated alert is a new message in the thread. `repeat_of` points at the
-- alert it repeats, so pressing Received on any of them stops the whole chain.

ALTER TABLE chat_messages ADD COLUMN repeat_of INTEGER;
