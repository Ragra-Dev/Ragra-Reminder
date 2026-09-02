-- 0002_task_relevance
--
-- Stores the section-relevance decision computed during Classroom sync
-- (see ragra/relevance/engine.py). The engine itself stayed stateless in
-- Phase 1 deliberately; this is where its output becomes persistent.
--
-- The default is 'RELEVANT', not NULL and not 'UNKNOWN', because the
-- relevance contract is fail-open: only an unambiguous match to a
-- *different* section may ever suppress a notification. Defaulting every
-- pre-existing row to RELEVANT means applying this migration cannot hide
-- a single already-synced task, which is the one outcome that would make
-- this feature worse than not having it.
--
-- relevance_reason is a short human-readable trace of why the decision was
-- reached, so a surprising suppression can be explained rather than
-- guessed at.

ALTER TABLE tasks ADD COLUMN relevance TEXT NOT NULL DEFAULT 'RELEVANT';
ALTER TABLE tasks ADD COLUMN relevance_reason TEXT;
ALTER TABLE tasks ADD COLUMN relevance_computed_at TEXT;
