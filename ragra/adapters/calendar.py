"""Calendar adapter: Ragra's OWN, narrowly-scoped Google Calendar
credential - separate from Hermes' Classroom credential.

Why a separate credential rather than reusing anything Hermes already has:
Hermes' broader Workspace skill requests full calendar/gmail-send/drive-write
access for the general agent's tool loop; that is far more privileged than
Ragra needs (create/update its own events) and belongs to a different trust
boundary. So Ragra requests only the calendar.events scope, on its own
token file, while still reusing the SAME already-registered Google OAuth
client (installed-app) so we are not standing up a second Google Cloud
project.

This mirrors hermes_cli.classroom.oauth's proven pattern exactly: load the
stored credential, refresh it silently if expired, persist the refreshed
credential back to disk, and only open a browser if none of that is
possible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

CALENDAR_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.events",
)


class CalendarAdapterError(RuntimeError):
    """Raised when Ragra's Calendar credential cannot be used without an
    interactive browser step."""


@dataclass(frozen=True)
class CalendarTokenPaths:
    oauth_client_file: Path  # the existing, already-registered Google OAuth client
    token_file: Path         # Ragra-owned, calendar-only, separate from Classroom's


def default_calendar_token_path(ragra_home: Path) -> Path:
    return ragra_home / "google_calendar_authorized_user.json"


def _secure_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _persist(creds: Any, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    _secure_file(token_file)


def calendar_auth_status(paths: CalendarTokenPaths) -> dict[str, Any]:
    """Non-secret status check: paths, scopes, and whether a usable
    credential already exists. Never opens a browser."""
    present = paths.token_file.exists()
    scopes: list[str] = []
    if present:
        try:
            data = json.loads(paths.token_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        raw_scopes = data.get("scopes") or data.get("scope") or []
        if isinstance(raw_scopes, str):
            raw_scopes = raw_scopes.split()
        scopes = list(raw_scopes)
    return {
        "oauth_client_present": paths.oauth_client_file.exists(),
        "token_present": present,
        "scopes": scopes,
        "has_required_scope": set(CALENDAR_SCOPES).issubset(set(scopes)),
    }


def ensure_calendar_credentials(paths: CalendarTokenPaths, *, interactive: bool = False) -> Any:
    """Load, silently refresh, or (only when interactive=True) newly
    authorize Ragra's Calendar credential."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise CalendarAdapterError("Google API client libraries are not installed.") from exc

    creds = None
    if paths.token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(paths.token_file), list(CALENDAR_SCOPES))
        except Exception as exc:
            if not interactive:
                raise CalendarAdapterError(
                    "Ragra's Calendar credential could not be loaded; interactive authorization is required."
                ) from exc
            creds = None

    if creds is not None and creds.valid:
        return creds

    if creds is not None and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            if not interactive:
                raise CalendarAdapterError(
                    "Refreshing Ragra's Calendar credential failed; interactive authorization is required."
                ) from exc
            creds = None
        else:
            _persist(creds, paths.token_file)
            return creds

    if not interactive:
        raise CalendarAdapterError(
            "Ragra has no usable Calendar credential yet; interactive authorization is required."
        )

    if not paths.oauth_client_file.exists():
        raise CalendarAdapterError(f"Missing Google OAuth client registration at {paths.oauth_client_file}")

    flow = InstalledAppFlow.from_client_secrets_file(str(paths.oauth_client_file), list(CALENDAR_SCOPES))
    creds = flow.run_local_server(port=0, prompt="consent")
    _persist(creds, paths.token_file)
    return creds


class CalendarClient(Protocol):
    def create_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def delete_event(self, calendar_id: str, event_id: str) -> None: ...
    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None: ...


class GoogleCalendarClient:
    """The only place that touches googleapiclient for Calendar operations."""

    def __init__(self, credentials: Any):
        from googleapiclient.discovery import build

        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def create_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._service.events().insert(calendarId=calendar_id, body=body).execute()

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._service.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        try:
            self._service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (404, 410):
                return
            raise

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        try:
            return self._service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                return None
            raise
