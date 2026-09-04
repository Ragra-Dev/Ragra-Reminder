"""The low-friction "Reconnect Google" action on the account page.

This is a *second*, separate OAuth purpose from `ragra/web/auth.py`'s
sign-in flow, and deliberately kept apart from it rather than merged in:
signing in to Ragra proves who you are (scopes: openid/email/profile,
never touching Classroom/Calendar); this module authorizes Ragra's server
to read/write a specific Google service (Classroom or Calendar) on an
*already signed-in* user's behalf. Conflating the two tables or the two
flows would make a login attempt and a data-authorization attempt
indistinguishable at the point either is consumed - exactly the kind of
ambiguity a security-sensitive callback must not have.

It reuses `ragra.web.auth`'s PKCE helpers (already generic, already
reviewed, not login-specific) rather than re-implementing them, and reuses
the exact same OAuth client registration Classroom/Calendar already use
(`ClassroomTokenPaths.credentials_file` / `CalendarTokenPaths.oauth_client_file`
- both point at the same file, confirmed in ragra/config.py) - a "Desktop
app" client, which Google accepts a loopback (http://127.0.0.1:<port>)
redirect for without pre-registering it, the same property
`InstalledAppFlow.run_local_server()` already relies on. No new Google
Cloud OAuth client is required.

This never bypasses Google's consent screen: the browser is sent to the
real accounts.google.com authorization endpoint exactly as
`ragra classroom-auth`/`calendar-auth` already do; the only thing that
changes is where the browser is sent *back to* (Ragra's own running web
server instead of a temporary local HTTP server that command spins up and
tears down).
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse

from ragra.db import repo
from ragra.tz import utc_iso
from ragra.web.auth import code_challenge_for, make_code_verifier

STATE_LIFETIME = timedelta(minutes=10)

CLASSROOM = "classroom"
CALENDAR = "calendar"
SERVICES = (CLASSROOM, CALENDAR)


class ReconnectError(RuntimeError):
    """A reconnect attempt could not be completed."""


class ReconnectNotConfigured(ReconnectError):
    """This deployment cannot start a reconnect at all - the OAuth client
    registration is missing/unreadable, or the configured redirect target
    is not one this client type may use.

    A distinct subclass so the web layer can answer with "the deployment
    is misconfigured" (503) rather than "your attempt failed" (400), and
    can do so without echoing the underlying message, which for a missing
    file would carry a local filesystem path."""


class InsufficientScopes(ReconnectError):
    """Google returned a credential that does not carry everything the
    requested service needs.

    Kept separate from a generic failure because the remedy is different
    and user-actionable: approve the whole consent screen rather than a
    subset of it."""

    def __init__(self, missing: tuple[str, ...]):
        self.missing = missing
        super().__init__("the authorization granted did not include every required scope")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _loopback(host: str | None) -> bool:
    """Whether a redirect host is this machine.

    Parsed as an address rather than string-compared, for the same reason
    ragra/web/app.py's `_is_loopback` does it: loopback is a whole /8 plus
    ::1, and "127.0.0.1" is only one member of it. `localhost` is accepted
    by name because Google documents it as a valid installed-app redirect
    host alongside the literal loopback addresses."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def assert_redirect_uri_allowed(*, client_type: str, redirect_uri: str) -> None:
    """Refuse a redirect target this OAuth client type must not be used with.

    Ragra's Classroom/Calendar registration is an *installed* (Desktop)
    client, which is a public client: its "secret" is not a secret, and
    Google only accepts loopback redirects for it. That combination is
    safe on a local machine and specifically unsafe anywhere else, so the
    unsafe shape is refused here rather than left to be noticed later -
    the configuration that would produce it (a hosted base URL, or binding
    the dashboard to 0.0.0.0) is exactly the change Phase 4 will make.

    A `web` client registration is the correct type for a hosted origin
    and is allowed a non-loopback https redirect.
    """
    parsed = urlparse(redirect_uri)

    if client_type == "installed":
        if parsed.scheme != "http" or not _loopback(parsed.hostname):
            raise ReconnectNotConfigured(
                "this deployment's Google OAuth client is a Desktop (installed) client, which may "
                "only redirect to a loopback address; configure a Web client registration before "
                "serving reconnect from a non-local address"
            )
        return

    if parsed.scheme != "https" or not parsed.hostname:
        raise ReconnectNotConfigured("a Web OAuth client requires an https redirect target")


class GoogleDataAuthProvider(Protocol):
    """The only part of reconnecting that talks to Google - isolated so the
    state/PKCE/ownership logic below is testable without a network round
    trip, same reasoning as ragra.web.auth.IdentityProvider."""

    def authorization_url(self, *, service: str, state: str, code_challenge: str) -> str: ...

    def exchange_code(self, *, service: str, code: str, code_verifier: str): ...


def begin_reconnect(
    conn: sqlite3.Connection,
    provider: GoogleDataAuthProvider,
    *,
    user_id: int,
    service: str,
    now: datetime,
) -> str:
    """Start a reconnect attempt for one already-signed-in user's one
    service, and return the URL to send the browser to.

    `prompt=consent`/`access_type=offline` (set by the real provider) are
    required every time: Google only returns a refresh token on first
    consent or when consent is forced, and a reconnect exists specifically
    to obtain a fresh one. This is not "unnecessary repeated consent" -
    it is the one moment a fresh grant is actually needed, triggered only
    by an explicit click, never automatically."""
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}")

    state = secrets.token_urlsafe(32)
    verifier = make_code_verifier()

    # Build the URL *before* recording the attempt. The provider can fail
    # here (a missing client registration, a redirect target this client
    # type may not use), and a row written first would then be an attempt
    # nobody can ever complete - harmless but permanent until it expires.
    url = provider.authorization_url(
        service=service, state=state, code_challenge=code_challenge_for(verifier)
    )

    repo.begin_google_reconnect(
        conn,
        state_hash=_hash(state),
        user_id=user_id,
        service=service,
        code_verifier=verifier,
        now=utc_iso(now),
        expires_at=utc_iso(now + STATE_LIFETIME),
    )

    return url


def required_scopes(service: str) -> tuple[str, ...]:
    """What a credential must carry to be usable for this service."""
    from ragra.adapters.calendar import CALENDAR_SCOPES
    from ragra.adapters.classroom import CLASSROOM_SCOPES

    return {CLASSROOM: CLASSROOM_SCOPES, CALENDAR: CALENDAR_SCOPES}[service]


def missing_scopes(*, service: str, granted) -> tuple[str, ...]:
    """Which of the service's required scopes are absent from `granted`.

    Checked before a credential is stored, so a grant that would fail at
    the first API call is refused while the user is still in front of the
    consent screen - rather than being recorded as HEALTHY and failing
    silently on some later unattended tick. It also prevents a credential
    obtained for one service being filed under the other: those scope sets
    are disjoint, so a calendar grant stored as `classroom` is caught here
    rather than becoming a confusing 403 much later.
    """
    have = set(granted or ())
    return tuple(scope for scope in required_scopes(service) if scope not in have)


@dataclass(frozen=True)
class ConsumedReconnect:
    user_id: int
    service: str
    code_verifier: str


def consume_reconnect(conn: sqlite3.Connection, *, state: str | None, now: datetime) -> ConsumedReconnect:
    """Validate and spend a reconnect state exactly once - same replay
    protection as ragra.web.auth.consume_state, and for the same reason:
    deleting before returning is what makes a captured callback URL
    unusable a second time."""
    if not state:
        raise ReconnectError("reconnect could not be completed")

    row = repo.consume_google_reconnect(conn, state_hash=_hash(state))
    if row is None or utc_iso(now) >= row["expires_at"]:
        raise ReconnectError("reconnect could not be completed")

    return ConsumedReconnect(
        user_id=row["user_id"], service=row["service"], code_verifier=row["code_verifier"]
    )


class GoogleOAuthDataProvider:
    """The real provider: Classroom/Calendar's own already-registered
    Desktop-type OAuth client, addressed via the standard web
    authorization-code flow instead of the loopback-local-server flow
    `ragra classroom-auth`/`calendar-auth` use. Same client, same scopes,
    same consent screen - only the redirect target differs."""

    AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

    def __init__(self, *, client_secrets_file, redirect_uri: str, classroom_scopes, calendar_scopes):
        self._client_secrets_file = client_secrets_file
        self._redirect_uri = redirect_uri
        self._scopes_by_service = {CLASSROOM: tuple(classroom_scopes), CALENDAR: tuple(calendar_scopes)}

    def _client(self) -> tuple[str, str, str]:
        """(client_id, client_secret, client_type), with the redirect
        target validated against the client type.

        Every failure here is a deployment problem rather than a user one,
        and each is raised as ReconnectNotConfigured *without* the
        underlying message - a missing-file error carries a local
        filesystem path, which has no business reaching a browser."""
        import json

        try:
            data = json.loads(self._client_secrets_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReconnectNotConfigured("no Google OAuth client registration is configured") from exc
        except (OSError, ValueError) as exc:
            raise ReconnectNotConfigured("the Google OAuth client registration could not be read") from exc

        client_type = "installed" if "installed" in data else "web" if "web" in data else ""
        block = data.get(client_type) or {}
        client_id, client_secret = block.get("client_id"), block.get("client_secret")
        if not client_type or not client_id or not client_secret:
            raise ReconnectNotConfigured("the Google OAuth client registration is incomplete")

        assert_redirect_uri_allowed(client_type=client_type, redirect_uri=self._redirect_uri)
        return client_id, client_secret, client_type

    def authorization_url(self, *, service: str, state: str, code_challenge: str) -> str:
        from urllib.parse import urlencode

        client_id, _, _ = self._client()
        params = {
            "client_id": client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._scopes_by_service[service]),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{self.AUTH_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, *, service: str, code: str, code_verifier: str):
        import requests
        from google.oauth2.credentials import Credentials

        client_id, client_secret, _ = self._client()
        response = requests.post(
            self.TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            timeout=30,
        )
        if response.status_code != 200:
            # The response body can echo request parameters; never surfaced or logged.
            raise ReconnectError("reconnect could not be completed")

        payload = response.json()
        if "refresh_token" not in payload:
            # Should not happen with access_type=offline + prompt=consent, but
            # a credential this module hands back must always be one the
            # unattended tick can actually use later - never a
            # access-token-only credential masquerading as a full one.
            raise ReconnectError(
                "Google did not return a refresh token - reconnect could not be completed"
            )

        return Credentials(
            token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            token_uri=self.TOKEN_ENDPOINT,
            client_id=client_id,
            client_secret=client_secret,
            scopes=payload.get("scope", "").split(),
        )
