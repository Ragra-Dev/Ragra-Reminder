"""The single boundary between Google OAuth credentials and storage.

Every path that saves or loads a user's Google authorization goes through
here, which is what makes "credentials are encrypted at rest" a property of
the system rather than a habit. `ragra/db/repo.py` only ever sees an opaque
blob; nothing else in the codebase calls `ragra/crypto.py` for credentials.

Credentials are serialised with `Credentials.to_json()`, the same format
the file-based flow already produced, so an existing on-disk token can be
imported without a re-consent (see `import_from_file`). That migration path
matters: telling the user to re-authorize because the storage layout
changed would be a self-inflicted wound.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ragra import crypto
from ragra.db import repo

CLASSROOM = "classroom"
CALENDAR = "calendar"
SERVICES = (CLASSROOM, CALENDAR)


class CredentialsNotStored(RuntimeError):
    """This user has not authorized this service."""


def _scopes_of(payload: str) -> str:
    """Read the granted scopes out of a serialised credential.

    Best-effort by design: the scopes are stored for display, so a payload
    shaped unexpectedly should cost a status line, not the ability to store
    a working credential.
    """
    try:
        scopes = json.loads(payload).get("scopes") or []
    except (ValueError, AttributeError):
        return ""
    return " ".join(str(scope) for scope in scopes)


def store(
    conn: sqlite3.Connection, *, user_id: int, service: str, payload: str, key: bytes | None = None
) -> None:
    """Encrypt and save one user's authorization for one service.

    Raises before writing anything if no key is configured. Storing the
    token in the clear "just this once" is exactly the silent downgrade
    ragra/crypto.py refuses to allow.
    """
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}")

    ciphertext = crypto.encrypt(payload, user_id=user_id, service=service, key=key)
    repo.store_google_credentials(
        conn,
        user_id=user_id,
        service=service,
        ciphertext=ciphertext,
        scopes=_scopes_of(payload),
    )


def load(
    conn: sqlite3.Connection, *, user_id: int, service: str, key: bytes | None = None
) -> str:
    """Return the decrypted credential payload for one user and service."""
    row = repo.get_google_credentials(conn, user_id=user_id, service=service)
    if row is None:
        raise CredentialsNotStored(f"no {service} authorization stored for this account")
    return crypto.decrypt(row["ciphertext"], user_id=user_id, service=service, key=key)


def has_credentials(conn: sqlite3.Connection, *, user_id: int, service: str) -> bool:
    """Whether an authorization exists, without decrypting it.

    Answerable without the key on purpose: a scheduler deciding whether a
    user is worth visiting should not have to unseal a secret to find out.
    """
    return repo.get_google_credentials(conn, user_id=user_id, service=service) is not None


def forget(conn: sqlite3.Connection, *, user_id: int, service: str | None = None) -> int:
    """Remove stored authorization. Local only - it does not revoke the
    grant at Google, and callers that mean "revoke everywhere" must say so
    to Google as well."""
    return repo.delete_google_credentials(conn, user_id=user_id, service=service)


def import_from_file(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    service: str,
    path: Path,
    key: bytes | None = None,
) -> bool:
    """Adopt an existing on-disk token into the encrypted per-user store.

    Returns False when there is nothing to import, so this is safe to call
    unconditionally during setup. Deliberately does not delete the source
    file: destroying the only copy of a working authorization as a side
    effect of a storage change would be an unpleasant surprise, and the
    file can be removed once the new path is confirmed working.
    """
    if not path.exists():
        return False
    payload = path.read_text(encoding="utf-8").strip()
    if not payload:
        return False
    store(conn, user_id=user_id, service=service, payload=payload, key=key)
    return True


def status(conn: sqlite3.Connection, *, user_id: int) -> dict[str, str]:
    """A safe-to-print summary. Never includes a token, a ciphertext, or
    anything derived from either - this is written to be pasted into a
    support conversation."""
    summary: dict[str, str] = {
        "encryption_key_configured": "yes" if crypto.is_configured() else "no",
    }
    for service in SERVICES:
        scopes = repo.google_credential_scopes(conn, user_id=user_id, service=service)
        summary[service] = f"authorized ({scopes})" if scopes is not None else "not authorized"
    return summary
