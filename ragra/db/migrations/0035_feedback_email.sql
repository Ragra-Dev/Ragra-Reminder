-- 0035_feedback_email
--
-- Every piece of feedback is also emailed to the team. These two columns record
-- what happened, so the owner's inbox can show "emailed" or why it was not:
--   emailed_at  when at least one team member's email was accepted for sending
--   email_note  a short plain-words result ("Sent to 2 of 2", or the reason it failed)

ALTER TABLE feedback ADD COLUMN emailed_at TEXT;
ALTER TABLE feedback ADD COLUMN email_note TEXT;
