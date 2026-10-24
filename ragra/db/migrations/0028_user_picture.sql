-- 0028_user_picture
--
-- The Google profile picture address, shown on the Account page and in the
-- navigation bar. Informational only, like `email` and `display_name`: it is
-- refreshed on every sign-in and is never used as an identity key. NULL until
-- the user next signs in (existing accounts have no picture recorded yet).

ALTER TABLE users ADD COLUMN picture_url TEXT;
