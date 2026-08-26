"""Classroom adapter: Ragra's OWN Google OAuth handling and read-only
Classroom API client. No Hermes import of any kind - this is Core Ragra
and must work whether or not a Hermes installation exists on the machine.

This mirrors ragra/adapters/calendar.py's already-proven pattern exactly:
load the stored credential, refresh it silently if expired, persist the
refreshed credential back to disk, and only open a browser if none of that
is possible. The credential file location defaults to where it already
lives today (see ragra/config.py's shared OAuth-client-directory default,
also used by the Calendar adapter) purely so the existing, already-granted
authorization keeps working without a new consent flow - that default is a
file-location convenience, not a code dependency on anything else.

One real-world wrinkle this module works around: for this Google Cloud
project, Google's consent screen only ever grants 4 of the 5 requested
Classroom scopes (classroom.coursework.me.readonly is a "restricted" scope
that Google's Console has not made grantable here - a Console-side
configuration limitation, not a code bug). RFC 6749 SS3.3 explicitly allows
a server to grant a scope subset; the client is expected to adapt, not
treat it as fatal. Two concrete adaptations follow from that:

1. The ONE-TIME interactive authorization (get_classroom_client(...,
   interactive=True)) sets OAUTHLIB_RELAX_TOKEN_SCOPE=1 - oauthlib's own
   documented mechanism for tolerating a granted-scope subset - so the
   token exchange saves whatever Google actually granted instead of
   crashing on the mismatch.
2. Routine, non-interactive use (the default) does not re-derive the full
   5-scope wishlist and fail closed if the stored token has fewer, which
   would force a doomed reauth loop here (re-authorizing can never produce
   a 5th scope Google's Console won't grant). Instead this module loads
   the stored token and trusts whatever scopes are actually recorded on
   it, refreshing silently.

This module never opens a browser unless the caller explicitly asks for
that (interactive=True); the default path only ever does a silent
load-or-refresh.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


CLASSROOM_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
)


class ClassroomAdapterError(RuntimeError):
    """Raised when the existing Classroom credential cannot be used without
    an interactive browser step."""


class ClassroomGoogleError(RuntimeError):
    """Raised when a Google API call fails in a user-safe way."""


@dataclass(frozen=True)
class ClassroomTokenPaths:
    credentials_file: Path  # OAuth client registration (shared with the Calendar adapter)
    token_file: Path
    legacy_token_file: Path


def default_classroom_token_paths(base_dir: Path) -> ClassroomTokenPaths:
    return ClassroomTokenPaths(
        credentials_file=base_dir / "credentials.json",
        token_file=base_dir / "token_classroom.json",
        legacy_token_file=base_dir / "token.json",
    )


def _existing_token_path(paths: ClassroomTokenPaths) -> Path | None:
    if paths.token_file.exists():
        return paths.token_file
    if paths.legacy_token_file.exists():
        return paths.legacy_token_file
    return None


def classroom_auth_status(paths: ClassroomTokenPaths) -> dict[str, Any]:
    """Non-secret status check: paths, scopes actually on the stored token,
    and whether it looks usable. Reports against reality (what's actually
    stored) rather than an aspirational full-scope wishlist, since a
    permanently-reduced grant here is expected, not an error state. Safe to
    call anywhere; never opens a browser."""
    token_path = _existing_token_path(paths)

    if token_path is None:
        return {
            "credentials_present": paths.credentials_file.exists(),
            "token_present": False,
            "token_path": None,
            "granted_scopes": [],
            "usable": False,
            "message": "No Classroom credential found. Run: ragra classroom-auth",
        }

    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    granted = data.get("scopes") or []

    return {
        "credentials_present": paths.credentials_file.exists(),
        "token_present": True,
        "token_path": str(token_path),
        "granted_scopes": granted,
        "usable": bool(granted),
        "message": "Classroom credential present." if granted else "Stored credential has no recorded scopes.",
    }


def _persist_token(creds: Any, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


def _load_and_refresh(paths: ClassroomTokenPaths) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = _existing_token_path(paths)
    if token_path is None:
        raise ClassroomAdapterError("No Classroom credential found. Run: ragra classroom-auth")

    try:
        # No explicit scopes= override: trust whatever scopes are actually
        # recorded on the stored token, which reflects what Google really
        # granted - not an aspirational list that may exceed what this
        # Google Cloud project can ever be granted.
        creds = Credentials.from_authorized_user_file(str(token_path))
    except Exception as exc:
        raise ClassroomAdapterError(
            "Stored Classroom credential could not be loaded. Run: ragra classroom-auth"
        ) from exc

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise ClassroomAdapterError(
                "Classroom credential refresh failed. Run: ragra classroom-auth"
            ) from exc
        _persist_token(creds, paths.token_file)
        return creds

    raise ClassroomAdapterError(
        "Classroom credential is not usable and cannot be refreshed. Run: ragra classroom-auth"
    )


def _run_interactive_consent(paths: ClassroomTokenPaths) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not paths.credentials_file.exists():
        raise ClassroomAdapterError(f"Missing Google OAuth client credentials at {paths.credentials_file}")

    # RFC 6749 SS3.3: a server MAY grant a scope subset; oauthlib's own
    # documented opt-in for tolerating that, rather than treating it as a
    # fatal client error. Scoped to this process only.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(str(paths.credentials_file), list(CLASSROOM_SCOPES))
    creds = flow.run_local_server(port=0, prompt="consent")
    _persist_token(creds, paths.token_file)
    return creds


def get_classroom_client(paths: ClassroomTokenPaths, *, interactive: bool = False) -> "ClassroomGoogleClient":
    """Return an authenticated ClassroomGoogleClient.

    interactive=False (the default): loads the existing token and silently
    refreshes it if expired, tolerant of a permanently-reduced granted-scope
    set (see module docstring). Raises ClassroomAdapterError if that isn't
    possible. Never opens a browser.

    interactive=True: opens a real browser consent screen. Callers must
    only pass this after getting explicit human go-ahead.
    """
    if interactive:
        try:
            creds = _run_interactive_consent(paths)
        except ClassroomAdapterError:
            raise
        except Exception as exc:
            raise ClassroomAdapterError(str(exc)) from exc
        return ClassroomGoogleClient(creds)

    creds = _load_and_refresh(paths)
    return ClassroomGoogleClient(creds)


class ClassroomGoogleClient:
    """The only place that touches googleapiclient for Classroom. Read-only
    by construction - no insert/update/delete/patch method exists here for
    any Classroom resource."""

    def __init__(self, credentials: Any):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise ClassroomGoogleError(
                "Google API client libraries are not installed."
            ) from exc

        self.credentials = credentials
        self._service = build("classroom", "v1", credentials=credentials, cache_discovery=False)

    def _execute(self, request: Any) -> dict[str, Any]:
        try:
            return request.execute()
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            reason = getattr(exc, "reason", None) or exc.__class__.__name__
            if status:
                raise ClassroomGoogleError(f"Google API request failed with HTTP {status}: {reason}") from exc
            raise ClassroomGoogleError(f"Google API request failed: {reason}") from exc

    def _list_all(self, factory: Callable[[str | None], Any], items_key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            response = self._execute(factory(page_token))
            page_items = response.get(items_key) or []
            if isinstance(page_items, list):
                items.extend(page_items)
            page_token = response.get("nextPageToken")
            if not page_token:
                return items

    def list_courses(self) -> list[dict[str, Any]]:
        return self._list_all(
            lambda page_token: self._service.courses().list(pageSize=100, pageToken=page_token),
            "courses",
        )

    def list_course_work(self, course_id: str) -> list[dict[str, Any]]:
        return self._list_all(
            lambda page_token: self._service.courses()
            .courseWork()
            .list(courseId=course_id, pageSize=100, pageToken=page_token, courseWorkStates=["PUBLISHED"]),
            "courseWork",
        )

    def list_announcements(self, course_id: str) -> list[dict[str, Any]]:
        return self._list_all(
            lambda page_token: self._service.courses()
            .announcements()
            .list(courseId=course_id, pageSize=100, pageToken=page_token, announcementStates=["PUBLISHED"]),
            "announcements",
        )

    def list_course_materials(self, course_id: str) -> list[dict[str, Any]]:
        return self._list_all(
            lambda page_token: self._service.courses()
            .courseWorkMaterials()
            .list(
                courseId=course_id,
                pageSize=100,
                pageToken=page_token,
                courseWorkMaterialStates=["PUBLISHED"],
            ),
            "courseWorkMaterial",
        )
