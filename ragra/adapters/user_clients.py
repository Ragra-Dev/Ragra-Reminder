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


def classroom_client_for(
    conn: sqlite3.Connection, config: Config, *, user_id: int
) -> classroom_adapter.ClassroomGoogleClient:
    """An authenticated Classroom client for one user. Never opens a
    browser - an unattended run must never block on a consent screen."""
    creds = _stored_credentials(
        conn,
        user_id=user_id,
        service=google_credentials.CLASSROOM,
        scopes=classroom_adapter.CLASSROOM_SCOPES,
    )
    if creds is not None:
        return classroom_adapter.ClassroomGoogleClient(creds)

    if not _is_legacy_owner(conn, user_id):
        raise UserCredentialsUnavailable("no Classroom authorization stored for this account")

    try:
        return classroom_adapter.get_classroom_client(config.classroom_paths, interactive=False)
    except classroom_adapter.ClassroomAdapterError as exc:
        raise UserCredentialsUnavailable(str(exc)) from exc


def calendar_client_for(
    conn: sqlite3.Connection, config: Config, *, user_id: int
) -> calendar_adapter.GoogleCalendarClient:
    """An authenticated Calendar client for one user. Never opens a
    browser, for the same reason as above."""
    creds = _stored_credentials(
        conn,
        user_id=user_id,
        service=google_credentials.CALENDAR,
        scopes=calendar_adapter.CALENDAR_SCOPES,
    )
    if creds is not None:
        return calendar_adapter.GoogleCalendarClient(creds)

    if not _is_legacy_owner(conn, user_id):
        raise UserCredentialsUnavailable("no Calendar authorization stored for this account")

    try:
        legacy = calendar_adapter.ensure_calendar_credentials(
            config.calendar_paths, interactive=False
        )
    except calendar_adapter.CalendarAdapterError as exc:
        raise UserCredentialsUnavailable(str(exc)) from exc
    return calendar_adapter.GoogleCalendarClient(legacy)
