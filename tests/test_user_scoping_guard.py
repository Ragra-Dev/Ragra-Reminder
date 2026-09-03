"""Structural guard: no code path can touch a user-owned table without
naming an owner.

This is deliberately a static check over the source rather than a runtime
test. The isolation property P3 establishes is "every query is filtered by
user_id", and the way that property decays is not by an existing query
changing - it is by someone adding a *new* function next year that forgets.
A behavioural test only covers the paths it happens to exercise; this one
fails the moment an unscoped query is written, including in code no test
calls yet.

The rule: any function whose own SQL names a user-owned table must either
take a `user_id` parameter or resolve one into a local `user_id` (the shape
web routes and CLI commands use, where identity comes from the request or
the invocation rather than the caller).

Two markers name the only legitimate exceptions, so that each is explicit,
greppable, and reviewable rather than indistinguishable from an oversight:

  ragra:cross-user   - the function genuinely spans accounts. In practice
                       this is retention housekeeping that deletes strictly
                       by age and returns only a count; scoping it per user
                       would leave a departed user's rows behind forever.
  ragra:token-scoped - the function is keyed by an unguessable secret
                       rather than by an owner. Session lookup is the case:
                       it cannot take a user_id because resolving the user
                       is what it does. Its safety comes from the token
                       being 256 bits of CSPRNG output, and the row it
                       returns names the owner every later query is scoped
                       to.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "ragra"

# Every table that carries a user_id column (migrations 0010-0025). `users`
# itself is excluded: it *is* the owner table.
USER_OWNED_TABLES = (
    "courses",
    "tasks",
    "task_history",
    "reminders",
    "calendar_events",
    "sync_state",
    "pipeline_health",
    "tick_sessions",
    "timetable_events",
    "class_reminders",
    "notification_deliveries",
    "sessions",
    "google_credentials",
    "user_profiles",
    "notification_preferences",
)

_TABLE_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+(" + "|".join(USER_OWNED_TABLES) + r")\b",
    re.IGNORECASE,
)

CROSS_USER_MARKER = "ragra:cross-user"
TOKEN_SCOPED_MARKER = "ragra:token-scoped"
EXEMPTION_MARKERS = (CROSS_USER_MARKER, TOKEN_SCOPED_MARKER)


def _python_sources() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _own_source(source: str, node: ast.AST, tree: ast.AST) -> str:
    """The function's source with any nested function definitions removed.

    Without this, an enclosing factory like `create_app` would inherit every
    query its nested routes contain and be reported instead of the route
    that actually owns it.
    """
    segment = ast.get_source_segment(source, node) or ""
    nested = [
        child
        for child in ast.iter_child_nodes(node)
        for child in ast.walk(child)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for inner in nested:
        inner_segment = ast.get_source_segment(source, inner)
        if inner_segment:
            segment = segment.replace(inner_segment, "")
    return segment


def _binds_user_id(node: ast.AST) -> bool:
    """True if the function takes a `user_id` argument or assigns one."""
    args = node.args
    if any(
        a.arg == "user_id"
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    ):
        return True
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            targets = child.targets
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
            targets = [child.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "user_id":
                return True
    return False


def _unscoped_functions() -> list[str]:
    offenders: list[str] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            segment = _own_source(source, node, tree)
            tables = sorted({m.group(1).lower() for m in _TABLE_REFERENCE.finditer(segment)})
            if not tables:
                continue
            if _binds_user_id(node):
                continue
            docstring = ast.get_docstring(node) or ""
            if any(marker in docstring for marker in EXEMPTION_MARKERS):
                continue
            relative = path.relative_to(PACKAGE_ROOT.parent)
            offenders.append(f"{relative}:{node.lineno} {node.name} -> {', '.join(tables)}")
    return offenders


def test_every_query_on_a_user_owned_table_names_its_owner():
    offenders = _unscoped_functions()
    assert not offenders, (
        "these functions query a user-owned table without a user_id.\n"
        "Add a user_id parameter (or resolve one), or - only if the function\n"
        f"genuinely qualifies - document it with '{CROSS_USER_MARKER}' or\n"
        f"'{TOKEN_SCOPED_MARKER}' in its docstring:\n  " + "\n  ".join(offenders)
    )


def test_the_exemption_markers_are_used_sparingly():
    """An exemption is a hole in the property this file exists to enforce,
    so the count is asserted rather than left to drift. Raising this number
    should require a deliberate decision, not go unnoticed in a diff."""
    exempted = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node) or ""
            if any(marker in docstring for marker in EXEMPTION_MARKERS):
                exempted.append(f"{path.name}:{node.name}")
    assert sorted(exempted) == [
        "repo.py:purge_old_tick_sessions",
        "sessions.py:lookup_session",
        "sessions.py:purge_expired_sessions",
        "sessions.py:revoke_session",
    ], f"the set of scoping exemptions changed: {sorted(exempted)}"


def test_the_guard_actually_detects_an_unscoped_query(tmp_path):
    """The guard is only worth having if it can fail. This proves the
    detection logic itself works, rather than trusting that an empty result
    means 'clean' when it might mean 'looked at nothing'."""
    module = ast.parse(
        "def leaky(conn):\n"
        "    return conn.execute('SELECT * FROM tasks').fetchall()\n"
    )
    node = module.body[0]
    assert not _binds_user_id(node)
    assert _TABLE_REFERENCE.search("SELECT * FROM tasks")


def test_the_guard_is_looking_at_real_files():
    """A path typo would make every assertion above vacuously pass."""
    sources = _python_sources()
    assert len(sources) > 15
    assert any(path.name == "repo.py" for path in sources)


@pytest.mark.parametrize("table", USER_OWNED_TABLES)
def test_every_listed_table_actually_has_a_user_id_column(conn, table):
    """Keeps this file's table list honest against the schema: a table that
    lost its user_id column would otherwise be silently over-enforced, and a
    new user-owned table added to the schema without being listed here would
    go unguarded."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    assert columns, f"{table} does not exist in the schema"
    assert "user_id" in columns, f"{table} has no user_id column"


def test_no_user_owned_table_is_missing_from_the_guard_list(conn):
    """The other direction: a future migration that adds a user_id column to
    a new table must also add it to USER_OWNED_TABLES, or the guard would
    quietly stop covering it."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    owned = {
        table
        for table in tables
        if any(row[1] == "user_id" for row in conn.execute(f"PRAGMA table_info({table})"))
    }
    assert owned == set(USER_OWNED_TABLES), (
        "USER_OWNED_TABLES is out of step with the schema; "
        f"missing from the guard: {sorted(owned - set(USER_OWNED_TABLES))}, "
        f"listed but no longer owned: {sorted(set(USER_OWNED_TABLES) - owned)}"
    )
