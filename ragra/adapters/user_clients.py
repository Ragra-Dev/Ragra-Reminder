"""Authenticated Google clients for a *particular user*.

Ragra's Classroom and Calendar adapters were written around file paths,
which is right for one user and cannot express several: a path has no
owner. This module is the seam between them and per-user identity. It
resolves a user's credential from the encrypted store (P3-6), refreshes it
when needed, writes the refreshed token straight back encrypted, and hands
the adapters an already-authenticated credential object.

The legacy file path is kept, deliberately and narrowly: when a user has
nothing in the store *and* is the pre-identity owner, the on-disk token is
used exactly as before. That is the account whose token those files have
always held, and breaking its sync because storage moved would be a
self-inflicted wound. Every other user must have a stored credential -
there is no path by which a second account can end up using the first
account's token file.

Refreshed tokens are persisted back to the store rather than left in
memory. Google refresh tokens can rotate, and dropping a rotated one turns
a working account into one that silently needs re-authorization on the next
restart.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ragra import crypto
from ragra.adapters import calendar as calendar_adapter
from ragra.adapters import classroom as classroom_adapter
from ragra.adapters import credential_health
from ragra.adapters import google_credentials
from ragra.config import Config
from ragra.db import repo


class UserCredentialsUnavailable(RuntimeError):
    """This user has no usable Google authorization for this service.

    A normal, expected state - a user who has not connected their Google
    account yet - so callers treat it as "skip this user", never as a
    failure of the run.
    """


def _is_legacy_owner(conn: sqlite3.Connection, user_id: int) -> bool:
    return repo.unlinked_user_id(conn) == user_id


def _credentials_from_payload(payload: str, scopes: tuple[str, ...]) -> Any:
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(json.loads(payload), list(scopes))


def _refresh_if_needed(
    conn: sqlite3.Connection, creds: Any, *, user_id: int, service: str
) -> Any:
    """Refresh an expired credential and write the result back encrypted.

    Persisting matters more than it looks: Google may rotate the refresh
    token during a refresh, and discarding a rotated one leaves an account
    that works until the process restarts and then silently does not.
    """
    if creds.valid:
        return creds
    if not (creds.expired and creds.refresh_token):
        raise UserCredentialsUnavailable(
            f"stored {service} authorization is not usable and cannot be refreshed"
        )

    from google.auth.transport.requests import Request

    try:
        creds.refresh(Request())
    except Exception as exc:  # noqa: BLE001 - any refresh failure means the same thing
        raise UserCredentialsUnavailable(
            f"refreshing the stored {service} authorization failed"
        ) from exc

    google_credentials.store(conn, user_id=user_id, service=service, payload=creds.to_json())
    return creds


def _stored_credentials(
    conn: sqlite3.Connection, *, user_id: int, service: str, scopes: tuple[str, ...]
) -> Any | None:
    """The user's credential from the encrypted store, or None if absent.

    A missing encryption key is reported as "unavailable" rather than
    crashing the run: on a deployment where the key is genuinely not
    configured, every user is simply skipped, which is the same outcome as
    nobody having connected an account.
    """
    if not google_credentials.has_credentials(conn, user_id=user_id, service=service):
        return None
    try:
        payload = google_credentials.load(conn, user_id=user_id, service=service)
    except (crypto.CredentialKeyMissing, crypto.CredentialDecryptionError) as exc:
        raise UserCredentialsUnavailable(
            f"stored {service} authorization could not be read"
        ) from exc

    creds = _credentials_from_payload(payload, scopes)
    return _refresh_if_needed(conn, creds, user_id=user_id, service=service)


def _resolve_client(
    conn: sqlite3.Connection,
    config: Config,
    *,
    user_id: int,
    service: str,
    scopes: tuple[str, ...],
    legacy_paths: Any,
    legacy_load,
    legacy_error_type: type[Exception],
    no_credential_error_type: type[Exception],
    wrap_client,
):
    """Shared body of classroom_client_for/calendar_client_for.

    Every exit path - success, a genuine failure, or "nothing stored yet"
    - is recorded through ragra/adapters/credential_health.py before
    returning or raising, so the account page and health alerting always
    reflect what actually just happened, for either storage backend. This
    is the one seam that knows both the user_id and the outcome of a real
    Google interaction, which is why the recording lives here rather than
    in classroom.py/calendar.py (which are user-agnostic, path-based) or
    in cli.py (which only sees an opaque exception by the time it's
    involved).

    `legacy_load` must return the raw credential object, never a client
    wrapped around one: this function wraps exactly once, at the end, and
    a loader that wrapped as well would produce a client built around
    another client rather than around a credential.

    **Storage precedence, stated once so it cannot be inferred wrongly:**
    the encrypted per-user store always wins. The on-disk legacy file is
    consulted only when that store holds nothing for this user+service
    *and* the user is the pre-identity owner. On a successful legacy load
    the credential is adopted into the encrypted store immediately (when a
    key is configured), so the two can never drift into disagreeing about
    which one is current - after the first successful use there is exactly
    one live copy, and the file is a spent migration source rather than a
    second source of truth.
    """
    try:
        creds = _stored_credentials(conn, user_id=user_id, service=service, scopes=scopes)
    except UserCredentialsUnavailable as exc:
        credential_health.record_failure(conn, user_id=user_id, service=service, exc=exc.__cause__ or exc)
        raise

    if creds is not None:
        credential_health.record_success(conn, user_id=user_id, service=service)
        return wrap_client(creds)

    if not _is_legacy_owner(conn, user_id):
        credential_health.record_not_connected(conn, user_id=user_id, service=service)
        raise UserCredentialsUnavailable(f"no {service} authorization stored for this account")

    try:
        legacy_creds = legacy_load(legacy_paths)
    except no_credential_error_type as exc:
        # A distinct type, not just another message: no token has ever
        # existed at this path, which is NOT_CONNECTED - expected, not a
        # failure - never DEGRADED/REAUTH_REQUIRED.
        credential_health.record_not_connected(conn, user_id=user_id, service=service)
        raise UserCredentialsUnavailable(str(exc)) from exc
    except legacy_error_type as exc:
        credential_health.record_failure(conn, user_id=user_id, service=service, exc=exc.__cause__ or exc)
        raise UserCredentialsUnavailable(str(exc)) from exc

    _adopt_legacy_credential(conn, user_id=user_id, service=service, credentials=legacy_creds)
    credential_health.record_success(conn, user_id=user_id, service=service)
    return wrap_client(legacy_creds)


def _adopt_legacy_credential(conn: sqlite3.Connection, *, user_id: int, service: str, credentials: Any) -> None:
    """Move a working on-disk credential into the encrypted store the
    first time it is successfully used.

    This closes the drift window the file otherwise creates: without it,
    the encrypted store and the file are two independent copies, only one
    of which stays current as tokens rotate, and whichever is consulted
    later silently decides whether sync works. After adoption the store is
    authoritative (it is consulted first) and the file is a spent
    migration source.

    Best-effort by design: a deployment with no encryption key configured
    is a supported state (see ragra/adapters/google_credentials.py), and
    failing a working sync because the *optional* copy could not be
    written would be a self-inflicted outage. The credential in hand is
    already good; adoption is an optimisation of where it lives.
    """
    if not crypto.is_configured():
        return
    try:
        google_credentials.store(conn, user_id=user_id, service=service, payload=credentials.to_json())
    except Exception:  # noqa: BLE001 - never let bookkeeping break a working sync
        return


def classroom_client_for(
    conn: sqlite3.Connection, config: Config, *, user_id: int
) -> classroom_adapter.ClassroomGoogleClient:
    """An authenticated Classroom client for one user. Never opens a
    browser - an unattended run must never block on a consent screen."""
    return _resolve_client(
        conn,
        config,
        user_id=user_id,
        service=google_credentials.CLASSROOM,
        scopes=classroom_adapter.CLASSROOM_SCOPES,
        legacy_paths=config.classroom_paths,
        legacy_load=classroom_adapter.load_classroom_credentials,
        legacy_error_type=classroom_adapter.ClassroomAdapterError,
        no_credential_error_type=classroom_adapter.NoClassroomCredential,
        wrap_client=classroom_adapter.ClassroomGoogleClient,
    )


def calendar_client_for(
    conn: sqlite3.Connection, config: Config, *, user_id: int
) -> calendar_adapter.GoogleCalendarClient:
    """An authenticated Calendar client for one user. Never opens a
    browser, for the same reason as above."""
    return _resolve_client(
        conn,
        config,
        user_id=user_id,
        service=google_credentials.CALENDAR,
        scopes=calendar_adapter.CALENDAR_SCOPES,
        legacy_paths=config.calendar_paths,
        legacy_load=lambda paths: calendar_adapter.ensure_calendar_credentials(paths, interactive=False),
        legacy_error_type=calendar_adapter.CalendarAdapterError,
        no_credential_error_type=calendar_adapter.NoCalendarCredential,
        wrap_client=calendar_adapter.GoogleCalendarClient,
    )
