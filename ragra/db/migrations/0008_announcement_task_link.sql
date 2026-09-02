-- 0008_announcement_task_link
--
-- Links a personal task back to the announcement it was created from, so
-- the workflow "I read this announcement and turned it into something I
-- have to do" is recorded rather than inferred.
--
-- The link is also what makes the action idempotent: creating a task from
-- the same announcement twice finds the existing child instead of making a
-- second one, which matters because a double-submitted form is the normal
-- way that would otherwise happen.
--
-- Nullable and unconstrained by design: the overwhelming majority of tasks
-- have no parent, and a self-referential NOT NULL would be wrong for every
-- one of them.

ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER REFERENCES tasks(id);

CREATE INDEX IF NOT EXISTS idx_tasks_parent_task_id ON tasks(parent_task_id);
