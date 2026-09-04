"""Classifying a Google credential failure, and persisting the result.

The failure mode this exists to prevent: Google invalidates a refresh
token (`invalid_grant: Token has been expired or revoked.`), the refresh
code catches that error and replaces it with a fixed string
("...refresh failed. Run: ragra classroom-auth"), and in doing so throws
away the one piece of information that actually matters - whether this is
a dead credential that will never succeed again without fresh consent, or
a network blip that resolves itself on the next 15-minute tick. Every
layer above that point - health tracking, the account page,
notifications - inherits the same blindness, so a dead credential can
keep looking healthy indefinitely while synchronization is silently
stale.

This module is the fix for that specific blindness, not a general-purpose
error-handling layer: it does exactly one thing, classify a credential-
related exception into a `CredentialState` plus a short, secret-free
reason, and hand the caller enough to persist that classification via
`ragra/db/repo.py`'s credential_health functions. It never touches a
token, a client secret, or any request/response body beyond the specific
`error` field OAuth error responses are documented to carry - which is a
code like "invalid_grant", never a credential.
"""

from __future__ import annotations

import sqlite3
from enum import Enum

from ragra import crypto
from ragra.db import repo


class CredentialState(str, Enum):
    """The account page, health alerts, and tick logging all read this -
    never a raw exception, never a bare "did the last attempt succeed"
    bit. String-valued so it round-trips through SQLite and templates
    without a lookup table."""

    HEALTHY = "healthy"
    # Refreshing failed in a way that plausibly resolves itself (network,
    # timeout, a Google-side 5xx) - still being retried automatically,
    # nothing has been asked of the user yet.
    DEGRADED = "degraded"
    # Google has positively told us this credential is dead
    # (invalid_grant, unauthorized_client, ...). Retrying will not help;
    # only fresh consent will.
    REAUTH_REQUIRED = "reauth_required"
    # No credential has ever been stored for this user+service. Expected,
    # not a failure - a user who hasn't connected Calendar yet is not
    # "broken".
    NOT_CONNECTED = "not_connected"
    # The deployment itself is misconfigured (e.g. RAGRA_CREDENTIAL_KEY
    # missing) - an operator problem, not something reauthorizing fixes.
    CONFIG_ERROR = "config_error"


# Error codes Google's token endpoint documents as meaning "this grant is
# dead" (RFC 6749 §5.2 plus the specific codes Google's own OAuth server
# is known to return for a revoked/expired refresh token). Retrying these
# without new consent will never succeed.
_PERMANENT_ERROR_CODES = frozenset(
    {"invalid_grant", "unauthorized_client", "invalid_client", "access_denied", "deleted_client"}
)

# Error codes that describe a problem on Google's side, not the grant's
# validity - safe to keep retrying.
_TRANSIENT_ERROR_CODES = frozenset({"server_error", "temporarily_unavailable", "internal_failure"})


def _refresh_error_payload(exc: Exception) -> dict:
    """The structured `{'error': ..., 'error_description': ...}` body a
    google-auth RefreshError carries when Google's token endpoint returned
    one - never anything derived from the token itself. Best-effort: not
    every RefreshError has this shape."""
    for arg in exc.args:
        if isinstance(arg, dict):
            return arg
    return {}


def classify_credential_error(exc: Exception) -> tuple[CredentialState, str, str]:
    """Classify a credential-related exception.

    Returns (state, error_category, safe_detail). Never inspects anything
    that could contain a secret: only the exception's type and, for a
    RefreshError, Google's own `error` code - a fixed vocabulary of short
    words, not credential material.

    Deliberately conservative in both directions: a positively-identified
    permanent Google error (invalid_grant and friends) is the *only* path
    to REAUTH_REQUIRED, so an ambiguous or unrecognised failure is never
    used to demand reauthorization the user may not actually need to do.
    Everything else that isn't a config problem defaults to DEGRADED -
    "still trying, not yet asking anything of you" - which is the safer
    default for a case this classifier has not seen before.
    """
    if isinstance(exc, (crypto.CredentialKeyMissing, crypto.CredentialDecryptionError)):
        return (
            CredentialState.CONFIG_ERROR,
            "credential_key_missing",
            "the local credential encryption key is missing or invalid - an operator problem, not something reauthorizing fixes",
        )

    try:
        from google.auth.exceptions import RefreshError, TransportError
    except ImportError:  # pragma: no cover - google-auth is a hard dependency in practice
        RefreshError = TransportError = ()  # type: ignore[assignment]

    if RefreshError and isinstance(exc, RefreshError):
        payload = _refresh_error_payload(exc)
        error_code = str(payload.get("error") or "").strip()

        if error_code in _PERMANENT_ERROR_CODES:
            return (
                CredentialState.REAUTH_REQUIRED,
                error_code,
                f"Google rejected this credential ({error_code}) - fresh consent is required",
            )
        if error_code in _TRANSIENT_ERROR_CODES:
            return (
                CredentialState.DEGRADED,
                error_code,
                f"Google reported a transient error ({error_code}) - retrying automatically",
            )
        return (
            CredentialState.DEGRADED,
            error_code or "refresh_error_unrecognised",
            "refresh failed with an unrecognised response - treating as transient rather than assuming reauthorization is needed",
        )

    if TransportError and isinstance(exc, TransportError):
        return (CredentialState.DEGRADED, "transport_error", "network error contacting Google - likely transient")

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return (CredentialState.DEGRADED, "network_error", "network error - likely transient")

    return (
        CredentialState.DEGRADED,
        "unexpected_error",
        f"unexpected error ({type(exc).__name__}) - treating as transient pending investigation",
    )


def record_success(conn: sqlite3.Connection, *, user_id: int, service: str, now: str | None = None) -> None:
    repo.record_credential_attempt(
        conn, user_id=user_id, service=service, state=CredentialState.HEALTHY.value,
        outcome=repo.WORKED, now=now,
    )


def record_not_connected(conn: sqlite3.Connection, *, user_id: int, service: str, now: str | None = None) -> None:
    """Not a failure, and not a success either - see
    CredentialState.NOT_CONNECTED.

    Recorded as NOTHING_TO_TRY rather than as a success: there is no
    credential here, so nothing can have "last worked", and claiming
    otherwise made the account page show "Last worked: <now>" beneath a
    "not connected" badge."""
    repo.record_credential_attempt(
        conn, user_id=user_id, service=service, state=CredentialState.NOT_CONNECTED.value,
        outcome=repo.NOTHING_TO_TRY, now=now,
    )


def record_failure(
    conn: sqlite3.Connection, *, user_id: int, service: str, exc: Exception, now: str | None = None
) -> tuple[CredentialState, str, str]:
    """Classify `exc` and persist it. Returns the classification so the
    caller can decide how to log/report this attempt without
    re-classifying it."""
    state, category, detail = classify_credential_error(exc)
    repo.record_credential_attempt(
        conn,
        user_id=user_id,
        service=service,
        state=state.value,
        outcome=repo.FAILED,
        error_category=category,
        error_detail=detail,
        now=now,
    )
    return state, category, detail
