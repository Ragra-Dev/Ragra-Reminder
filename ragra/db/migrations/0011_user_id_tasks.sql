-- 0011_user_id_tasks
-- ragra:foreign-keys-off
--
-- tasks gains a direct user_id even though ownership is already derivable
-- through course_id -> courses.user_id. That redundancy is deliberate: a
-- derived owner can only be enforced by remembering to join, and a query that
-- forgets the join silently returns every user's rows. A direct NOT NULL
-- column makes the owner filterable without a join and impossible to omit
-- from the schema, which is the same reasoning docs/INTERFACES.md #5 already
-- applies to the manual/Classroom write guard.
--
-- The existing UNIQUE(course_id, source_type, external_id) needs no user_id:
-- course_id is itself user-scoped after 0010, so the tuple is already
-- per-user. It is preserved exactly as-is.
--
-- parent_task_id (0008) is self-referential and is copied verbatim along with
-- the ids it points at, so announcement -> personal-task links survive.

DROP TABLE IF EXISTS tasks_new;

CREATE TABLE tasks_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    source_type TEXT NOT NULL,             -- coursework | announcement | material | manual
    external_id TEXT,                      -- Classroom item id; NULL for manual tasks
    title TEXT NOT NULL,
    description TEXT,
    link TEXT,
    kind TEXT NOT NULL DEFAULT 'ACTIONABLE',   -- ACTIONABLE | INFORMATIONAL
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    actual_deadline TEXT,                  -- ISO 8601, authoritative (Classroom), NULL if none
    personal_deadline TEXT,                -- ISO 8601, the user's intended completion time
    source_published_at TEXT,
    source_updated_at TEXT,
    completed_at TEXT,
    missed_at TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    relevance TEXT NOT NULL DEFAULT 'RELEVANT',
    relevance_reason TEXT,
    relevance_computed_at TEXT,
    parent_task_id INTEGER REFERENCES tasks(id),
    UNIQUE(course_id, source_type, external_id)
);

INSERT INTO tasks_new
    (id, user_id, course_id, source_type, external_id, title, description, link, kind, status,
     actual_deadline, personal_deadline, source_published_at, source_updated_at, completed_at,
     missed_at, cancelled_at, created_at, updated_at, relevance, relevance_reason,
     relevance_computed_at, parent_task_id)
SELECT
    id,
    (SELECT id FROM users ORDER BY id LIMIT 1),
    course_id, source_type, external_id, title, description, link, kind, status,
    actual_deadline, personal_deadline, source_published_at, source_updated_at, completed_at,
    missed_at, cancelled_at, created_at, updated_at, relevance, relevance_reason,
    relevance_computed_at, parent_task_id
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_actual_deadline ON tasks(actual_deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_parent_task_id ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);
