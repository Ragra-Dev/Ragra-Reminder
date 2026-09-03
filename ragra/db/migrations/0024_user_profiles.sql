-- 0024_user_profiles
--
-- Each user's own academic profile: which program they are in, when they
-- started, and which course sections they are enrolled in. Until now this
-- lived as a hand-edited table in ragra/timetable/enrollment.py, which is
-- correct for one user and cannot work for several - a module constant has
-- no owner, so a second account would silently inherit the first one's
-- enrollment and be told which of someone else's classes are relevant to
-- them.
--
-- Deliberately created empty. The obvious alternative - seeding the
-- existing owner's enrollment as literal values here - would copy one
-- person's course list and section letters into a migration file in a
-- public repository, permanently. Instead ragra/relevance/profile.py falls
-- back to the module default for the pre-identity owner only, and every
-- other user configures their own. Nothing about the current user's
-- behaviour changes, and no personal data is added to the repository.
--
-- `enrollment` is JSON rather than a child table on purpose: it is read
-- whole, written whole, and never queried by its contents. A normalised
-- table would buy nothing and cost a join on every relevance decision.

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    program TEXT NOT NULL,                  -- e.g. CS
    batch_year TEXT,                        -- e.g. 2025
    enrollment_start_year INTEGER NOT NULL, -- drives expected_semester
    enrollment_start_term TEXT NOT NULL,    -- FALL | SPRING
    enrollment TEXT NOT NULL,               -- JSON array of enrolled courses
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
