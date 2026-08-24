"""Classroom adapter: a thin wrapper around the existing, already-decoupled
hermes_cli.classroom.{oauth,google_client} modules.

Ragra does not reimplement Google OAuth or the Classroom API wrapper here -
it imports Hermes' read-only Classroom code directly and reuses its
credential file location as-is (hermes_cli.classroom.oauth.build_oauth_paths()
remains the single source of truth for where the token/client-registration
files live and which scopes this app requests).

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
2. Routine, non-interactive use (the default) does NOT reuse Hermes'
   ensure_credentials(interactive=False): that function always re-derives
   the full 5-scope wishlist and fails closed if the stored token has
   fewer, which would force a doomed reauth loop here (re-authorizing can
   never produce a 5th scope Google's Console won't grant). Instead this
   module loads the stored token and trusts whatever scopes are actually
   recorded on it, refreshing silently exactly like Hermes' own code does.
   This still uses Hermes' file location/format - it is not a second
   credential store, just a more tolerant load step for a scope subset
   Hermes' stricter check was never designed to accept as final.

This module never opens a browser unless the caller explicitly asks for
that (interactive=True); the default path only ever does a silent
load-or-refresh.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


class ClassroomAdapterError(RuntimeError):
    """Raised when the existing Classroom credential cannot be used without
    an interactive browser step."""


def _hermes_classroom_modules(hermes_repo_path: Path):
    repo_str = str(hermes_repo_path)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    from hermes_cli.classroom import oauth
    from hermes_cli.classroom.google_client import ClassroomGoogleClient

    return oauth, ClassroomGoogleClient


def classroom_auth_status(hermes_repo_path: Path) -> dict[str, Any]:
    """Non-secret status check: paths, scopes actually on the stored token,
    and whether it looks usable. Reports against reality (what's actually
    stored) rather than Hermes' aspirational full-scope wishlist, since a
    permanently-reduced grant here is expected, not an error state. Safe to
    call anywhere; never opens a browser."""
    oauth, _ = _hermes_classroom_modules(hermes_repo_path)
    return _status_from_paths(oauth.build_oauth_paths())


def _status_from_paths(paths: Any) -> dict[str, Any]:
    import json

    token_path = paths.token_file if paths.token_file.exists() else paths.legacy_token_file

    if not token_path.exists():
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


def _load_and_refresh(paths: Any) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = paths.token_file if paths.token_file.exists() else paths.legacy_token_file
    if not token_path.exists():
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
        _persist_token(creds, token_path)
        return creds

    raise ClassroomAdapterError(
        "Classroom credential is not usable and cannot be refreshed. Run: ragra classroom-auth"
    )


def get_classroom_client(hermes_repo_path: Path, *, interactive: bool = False) -> Any:
    """Return an authenticated ClassroomGoogleClient.

    interactive=False (the default): loads the existing token and silently
    refreshes it if expired, tolerant of a permanently-reduced granted-scope
    set (see module docstring). Raises ClassroomAdapterError if that isn't
    possible.

    interactive=True: falls back to opening a real browser consent screen
    via Hermes' own flow. Callers must only pass this after getting
    explicit human go-ahead.
    """
    oauth, ClassroomGoogleClient = _hermes_classroom_modules(hermes_repo_path)

    if interactive:
        # RFC 6749 SS3.3: a server MAY grant a scope subset; oauthlib's own
        # documented opt-in for tolerating that, rather than treating it as
        # a fatal client error. Scoped to this process only.
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        try:
            creds = oauth.ensure_credentials(include_drive=False, interactive=True)
        except oauth.ClassroomAuthError as exc:
            raise ClassroomAdapterError(str(exc)) from exc
        return ClassroomGoogleClient(creds)

    paths = oauth.build_oauth_paths()
    creds = _load_and_refresh(paths)
    return ClassroomGoogleClient(creds)
