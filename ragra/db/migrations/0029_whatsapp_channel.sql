-- 0029_whatsapp_channel
--
-- A hosted WhatsApp channel (Meta WhatsApp Cloud API), separate from the
-- Hermes target which only works on a machine that has Hermes installed.
-- `whatsapp_to` is the person's own number, stored as digits only in
-- international form (no "+", no spaces), which is also the form Meta puts
-- in the `from` field of an inbound message - so the same column both says
-- where to send and identifies who is writing in. A number is not a secret.

ALTER TABLE notification_preferences ADD COLUMN whatsapp_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_preferences ADD COLUMN whatsapp_to TEXT;
