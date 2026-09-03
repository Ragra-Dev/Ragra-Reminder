"""Deleting an account.

"Delete my account" has to mean it. The failure this module is written
against is not a crash - it is a deletion that looks successful and leaves
something behind: a live Google grant, a session that still works, a
notification destination that keeps receiving reminders for work nobody can
see any more. Each of those is worse than refusing to delete at all,
because the user has been told it is gone.

Completeness rests on ON DELETE CASCADE, declared on every table that
carries a user_id. That is the right mechanism - it cannot be forgotten by
a future function the way an explicit delete list can - but it has one
sharp edge: SQLite enforces it only when `PRAGMA foreign_keys` is on for
the connection. With it off, `DELETE FROM users` succeeds and silently
orphans everything. So the pragma is asserted rather than assumed (see
repo.delete_user), and tests/test_account_deletion.py enumerates the schema
to prove every owned table is actually reached, rather than checking the
handful somebody remembered to list.

Local deletion is not revocation at Google. Removing a stored credential
stops Ragra using it; it does not withdraw the grant from the user's Google
account. Saying so plainly is part of the feature - a user who believes
access was revoked when it was not has been misled by the product.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ragra.db import repo


@dataclass(frozen=True)
class DeletionSummary:
    """What was actually removed. Counts are gathered before the delete,
    because afterwards there is nothing left to count - and a summary that
    says "0 rows" for a full account would read as a failure."""

    user_id: int
    display_name: str | None
    rows_by_table: dict[str, int]
    sessions_revoked: int
    google_services_disconnected: int

    @property
    def total_rows(self) -> int:
        return sum(self.rows_by_table.values())


class UnknownAccount(RuntimeError):
    """No such account."""


def owned_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table carrying a user_id, read from the schema rather than
    listed here.

    Deliberately derived: a hand-maintained list is exactly the thing that
    goes stale the next time a table is added, and the symptom would be a
    deletion silently missing it.
    """
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return sorted(
        table
        for table in tables
        if any(column[1] == "user_id" for column in conn.execute(f"PRAGMA table_info({table})"))
    )


def preview_deletion(conn: sqlite3.Connection, *, user_id: int) -> DeletionSummary:
    """What deleting this account would remove, without removing it.

    Exists so the confirmation a user is shown is generated from the same
    schema walk the deletion uses, rather than being a hard-coded sentence
    that can quietly stop being true.
    """
    user = repo.get_user(conn, user_id=user_id)
    if user is None:
        raise UnknownAccount(f"no account with id {user_id}")

    counts = {}
    for table in owned_tables(conn):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]

    return DeletionSummary(
        user_id=user_id,
        display_name=user["display_name"],
        rows_by_table={table: count for table, count in counts.items() if count},
        sessions_revoked=counts.get("sessions", 0),
        google_services_disconnected=counts.get("google_credentials", 0),
    )


def delete_account(conn: sqlite3.Connection, *, user_id: int) -> DeletionSummary:
    """Delete an account and everything it owns.

    Returns what was removed. Raises UnknownAccount rather than reporting a
    cheerful success for an id that never existed - "deleted" and "was never
    here" are different answers, and only one of them means the caller's
    request was carried out.
    """
    summary = preview_deletion(conn, user_id=user_id)

    # Belt and braces with repo.delete_user's own assertion: enabling it
    # here means a caller on a connection that was opened without it still
    # gets a real cascade rather than a silent orphaning.
    conn.execute("PRAGMA foreign_keys=ON")
    if not repo.delete_user(conn, user_id=user_id):
        raise UnknownAccount(f"no account with id {user_id}")

    leftovers = {
        table: count
        for table in owned_tables(conn)
        if (
            count := conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (user_id,)
            ).fetchone()["c"]
        )
    }
    if leftovers:
        # Loud rather than tidy. A partial deletion is the one outcome a
        # user must never be told was a success, and it means a table was
        # added without ON DELETE CASCADE.
        raise RuntimeError(
            f"account {user_id} was deleted but rows remain in {sorted(leftovers)}; "
            "a user-owned table is missing ON DELETE CASCADE"
        )

    return summary


def describe(summary: DeletionSummary) -> list[str]:
    """Human-readable lines for a confirmation prompt or a receipt.

    Ends with what deletion does *not* do. A user who believes their Google
    access was revoked when it was only forgotten locally has been misled,
    and that is a product failure even though no code is wrong.
    """
    lines = [f"Account {summary.user_id}" + (f" ({summary.display_name})" if summary.display_name else "")]
    if summary.rows_by_table:
        lines.append(f"  {summary.total_rows} row(s) across {len(summary.rows_by_table)} table(s):")
        for table, count in sorted(summary.rows_by_table.items()):
            lines.append(f"    {table}: {count}")
    else:
        lines.append("  no stored data")

    if summary.sessions_revoked:
        lines.append(f"  {summary.sessions_revoked} sign-in session(s) will stop working immediately")
    if summary.google_services_disconnected:
        lines.append(
            f"  {summary.google_services_disconnected} stored Google authorization(s) will be destroyed"
        )
        lines.append(
            "  NOTE: this stops Ragra using that access. It does not withdraw the grant"
        )
        lines.append(
            "        from your Google account - do that at https://myaccount.google.com/permissions"
        )
    return lines
