"""Google sign-in: the OAuth round trip, identity resolution, and the
policy that decides who is allowed in at all.

The transport-specific half (building an authorization URL, exchanging a
code) sits behind the `IdentityProvider` protocol so the security-critical
half - state validation, PKCE, the allow-list, one-time adoption of the
pre-identity owner, session issuance - is exercised by tests without a
network round trip. A security property that can only be tested against
Google's live servers is a security property that does not get tested.

Three rules this module exists to enforce:

1. Identity is the Google `sub`, never the email address. A Google account's
   email can change while its subject id cannot, so matching on email would
   let one account silently inherit another's data. Email is used for
   exactly one thing - deciding, once, who may claim the pre-identity owner
   row - and that decision is configured out of band by the operator.

2. Sign-in is closed by default. An unlisted account is refused rather than
   silently given a new empty account, because this is a personal system
   and the interesting failure is not "someone couldn't sign in", it is
   "someone signed in".

3. Every authorization attempt is bound to the browser that started it, is
   single-use, and expires. A callback that cannot be matched to a live
   attempt is rejected without being told why.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse

from ragra.db import repo
from ragra.tz import utc_iso

# How long an unfinished sign-in attempt stays valid. Long enough to read a
# consent screen, short enough that abandoned attempts do not accumulate.
STATE_LIFETIME = timedelta(minutes=10)

# Only what is needed to identify the person. Ragra's Classroom and Calendar
# access is a separate authorization with its own scopes (P3-6) - signing in
# deliberately does not grant it, so someone can sign in to look at their
# dashboard without handing over access to their coursework.
LOGIN_SCOPES = ("openid", "email", "profile")


class AuthError(RuntimeError):
    """Sign-in could not be completed."""


class AuthNotConfigured(AuthError):
    """Sign-in is not set up on this deployment."""


class SignInRefused(AuthError):
    """The account is not permitted to sign in here."""


@dataclass(frozen=True)
class GoogleIdentity:
    """The verified claims Ragra uses. Nothing else from the ID token is
    kept: the point of sign-in is to learn who this is, not to accumulate
    profile data."""

    subject: str            # the 'sub' claim - the only identity key
    email: str | None       # informational, plus the one-time owner claim
    email_verified: bool
    display_name: str | None


@dataclass(frozen=True)
class AuthSettings:
    """Sign-in configuration, resolved from the environment.

    `owner_email` is the account permitted to adopt the pre-identity owner
    row - the row every pre-P3 record belongs to. Without it, sign-in fails
    closed rather than letting whoever arrives first claim that history.
    """

    client_id: str
    client_secret: str
    redirect_uri: str
    owner_email: str | None
    allowed_emails: frozenset[str]
    secure_cookies: bool

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


def _normalise_email(value: str | None) -> str | None:
    return value.strip().lower() or None if value else None


def load_auth_settings(env: dict[str, str] | None = None) -> AuthSettings:
    """Read sign-in configuration from the environment.

    Cookie `Secure` is derived from the redirect URI's scheme rather than
    configured separately, so a deployment served over HTTPS cannot end up
    issuing session cookies that a downgrade attack could read. It can be
    forced on, but deliberately not off for an https deployment.
    """
    source = os.environ if env is None else env
    redirect_uri = (source.get("RAGRA_OAUTH_REDIRECT_URI") or "").strip()
    forced_secure = (source.get("RAGRA_SECURE_COOKIES") or "").strip().lower() in ("1", "true", "yes")

    allowed = {
        email
        for email in (
            _normalise_email(part)
            for part in (source.get("RAGRA_ALLOWED_EMAILS") or "").split(",")
        )
        if email
    }

    return AuthSettings(
        client_id=(source.get("RAGRA_OAUTH_CLIENT_ID") or "").strip(),
        client_secret=(source.get("RAGRA_OAUTH_CLIENT_SECRET") or "").strip(),
        redirect_uri=redirect_uri,
        owner_email=_normalise_email(source.get("RAGRA_OWNER_EMAIL")),
        allowed_emails=frozenset(allowed),
        secure_cookies=forced_secure or redirect_uri.lower().startswith("https://"),
    )


class IdentityProvider(Protocol):
    """The only part of sign-in that talks to Google."""

    def authorization_url(self, *, state: str, code_challenge: str) -> str: ...

    def exchange_code(self, *, code: str, code_verifier: str) -> GoogleIdentity: ...


# ---------------------------------------------------------------------------
# PKCE and state
# ---------------------------------------------------------------------------


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_code_verifier() -> str:
    """A PKCE verifier: 43-128 unreserved characters (RFC 7636 §4.1)."""
    return secrets.token_urlsafe(64)[:128]


def code_challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier.

    Base64url without padding, per RFC 7636 §4.2. The plain method is never
    used: it would put the verifier itself in the authorization request,
    defeating the point.
    """
    import base64

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def safe_redirect_target(value: str | None) -> str:
    """Reduce a requested post-login destination to a safe local path.

    Anything with a scheme, a host, or a protocol-relative prefix is
    discarded rather than sanitised, because a redirect target is exactly
    the kind of value where "clean it up and carry on" turns into an open
    redirect. The default is the dashboard, which is always safe.

    Backslashes are rejected outright. `/\\evil.example` passes every check
    below - it starts with a single slash, and urlparse reports no scheme
    and no netloc - but browsers normalise the backslash to a forward
    slash, turning it into the protocol-relative `//evil.example` and
    navigating off-site. It is a known bypass of exactly this kind of
    check, and a backslash has no legitimate place in a Ragra path.
    """
    if not value:
        return "/"
    candidate = value.strip()
    if "\\" in candidate:
        return "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"
    return candidate


def begin_sign_in(
    conn: sqlite3.Connection,
    provider: IdentityProvider,
    *,
    now: datetime,
    redirect_to: str | None = None,
) -> str:
    """Start an authorization attempt and return the URL to send the browser
    to. The state and PKCE verifier are recorded server-side; only their
    public halves travel with the request."""
    state = secrets.token_urlsafe(32)
    verifier = make_code_verifier()

    conn.execute(
        """INSERT INTO oauth_states (state_hash, code_verifier, redirect_to, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            _hash(state),
            verifier,
            safe_redirect_target(redirect_to),
            utc_iso(now),
            utc_iso(now + STATE_LIFETIME),
        ),
    )
    conn.commit()

    return provider.authorization_url(state=state, code_challenge=code_challenge_for(verifier))


@dataclass(frozen=True)
class ConsumedState:
    code_verifier: str
    redirect_to: str


def consume_state(conn: sqlite3.Connection, *, state: str | None, now: datetime) -> ConsumedState:
    """Validate and spend a state value exactly once.

    Deleting before returning is what makes a captured callback URL
    unreplayable: the second attempt finds nothing. An unknown or expired
    state raises the same error as a missing one - the caller has no
    legitimate need for the difference, and reporting it would tell an
    attacker whether a guessed state was ever real.
    """
    if not state:
        raise AuthError("sign-in could not be completed")

    state_hash = _hash(state)
    row = conn.execute(
        "SELECT code_verifier, redirect_to, expires_at FROM oauth_states WHERE state_hash = ?",
        (state_hash,),
    ).fetchone()
    # Spent whether or not it turns out to be usable, so a state cannot be
    # probed repeatedly.
    conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
    conn.commit()

    if row is None or utc_iso(now) >= row["expires_at"]:
        raise AuthError("sign-in could not be completed")

    return ConsumedState(
        code_verifier=row["code_verifier"],
        redirect_to=safe_redirect_target(row["redirect_to"]),
    )


def purge_expired_states(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Housekeeping for abandoned sign-in attempts. Rows here belong to
    nobody, so there is no ownership to scope this to."""
    cur = conn.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (utc_iso(now),))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Policy and identity resolution
# ---------------------------------------------------------------------------


def is_allowed(identity: GoogleIdentity, settings: AuthSettings) -> bool:
    """Whether this verified identity may sign in.

    Closed by default: with no owner and no allow-list configured, nobody
    is admitted. An unverified email is never matched against either list,
    because an unverified address proves nothing about who controls it.
    """
    if not identity.email_verified or not identity.email:
        return False
    email = identity.email.strip().lower()
    return email == settings.owner_email or email in settings.allowed_emails


def resolve_user(
    conn: sqlite3.Connection, identity: GoogleIdentity, settings: AuthSettings, *, now: datetime
) -> int:
    """Map a verified Google identity to a Ragra user id.

    Three cases, in order:

    1. The subject is already linked - return that user. This is every
       sign-in after the first, and it never consults email, so changing
       your Google address does not change who you are here.

    2. The subject is new and this is the configured owner - adopt the
       pre-identity row, which is what keeps the entire pre-P3 history
       attached to its real owner instead of orphaning it behind a fresh
       empty account. Adoption happens at most once: the row stops being
       unlinked the moment it is claimed.

    3. The subject is new and allowed but is not the owner - create a new,
       empty account.
    """
    existing = repo.get_user_by_google_sub(conn, google_sub=identity.subject)
    if existing is not None:
        _refresh_profile(conn, user_id=existing["id"], identity=identity, now=now)
        return existing["id"]

    if not is_allowed(identity, settings):
        raise SignInRefused("this account is not permitted to sign in")

    email = (identity.email or "").strip().lower()
    if settings.owner_email and email == settings.owner_email:
        unlinked = repo.unlinked_user_id(conn)
        if unlinked is not None:
            conn.execute(
                """UPDATE users SET google_sub = ?, email = ?, display_name = ?, updated_at = ?
                   WHERE id = ? AND google_sub IS NULL""",
                (identity.subject, identity.email, identity.display_name, utc_iso(now), unlinked),
            )
            conn.commit()
            return unlinked

    cur = conn.execute(
        """INSERT INTO users (google_sub, email, display_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (identity.subject, identity.email, identity.display_name, utc_iso(now), utc_iso(now)),
    )
    conn.commit()
    return cur.lastrowid


def _refresh_profile(
    conn: sqlite3.Connection, *, user_id: int, identity: GoogleIdentity, now: datetime
) -> None:
    """Keep the informational fields current on each sign-in. google_sub is
    never rewritten here - it is the identity key, and a path that could
    change it would be a path that could move data between accounts."""
    conn.execute(
        "UPDATE users SET email = ?, display_name = ?, updated_at = ? WHERE id = ?",
        (identity.email, identity.display_name, utc_iso(now), user_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The Google-facing implementation
# ---------------------------------------------------------------------------


class GoogleIdentityProvider:
    """The real provider. Isolated here so nothing above it imports Google
    libraries, which is what lets the security logic be tested directly."""

    AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

    def __init__(self, settings: AuthSettings):
        if not settings.configured:
            raise AuthNotConfigured(
                "sign-in is not configured; set RAGRA_OAUTH_CLIENT_ID, "
                "RAGRA_OAUTH_CLIENT_SECRET and RAGRA_OAUTH_REDIRECT_URI"
            )
        self._settings = settings

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_uri,
            "response_type": "code",
            "scope": " ".join(LOGIN_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Sign-in only. No refresh token is requested because Ragra has
            # nothing to do with this authorization once it has learned who
            # the user is - long-lived Google access is a separate grant
            # with its own scopes.
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{self.AUTH_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, *, code: str, code_verifier: str) -> GoogleIdentity:
        import google.auth.transport.requests
        import requests
        from google.oauth2 import id_token as google_id_token

        response = requests.post(
            self.TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "redirect_uri": self._settings.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            timeout=30,
        )
        if response.status_code != 200:
            # The response body can echo request parameters; it is never
            # surfaced or logged.
            raise AuthError("sign-in could not be completed")

        raw_id_token = response.json().get("id_token")
        if not raw_id_token:
            raise AuthError("sign-in could not be completed")

        # Verifies signature against Google's published keys, plus issuer,
        # audience, and expiry. Decoding without verifying would make the
        # whole flow decorative - a forged token would sign anyone in.
        claims = google_id_token.verify_oauth2_token(
            raw_id_token,
            google.auth.transport.requests.Request(),
            self._settings.client_id,
        )
        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise AuthError("sign-in could not be completed")

        return GoogleIdentity(
            subject=claims["sub"],
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified")),
            display_name=claims.get("name"),
        )
