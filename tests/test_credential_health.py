"""Credential health: classification and the persisted state machine.

Covers the failure mode an earlier version of this suite missed entirely:
Google invalidating a refresh token (`invalid_grant: Token has been
expired or revoked.`) while every existing test only exercised local
preconditions ("no token file", "no refresh_token") - never "a
refresh_token is present and Google's server rejects it".
`test_a_rejected_refresh_token_is_not_silent` below is that missing case.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError, TransportError

from ragra import crypto
from ragra.adapters import credential_health, google_credentials
from ragra.adapters.credential_health import CredentialState, classify_credential_error
from ragra.adapters.user_clients import UserCredentialsUnavailable, classroom_client_for
from ragra.config import Config
from ragra.db import repo
from ragra.db.connection import connect_closing
from tests.support import owner_id

# The exact shape Google's token endpoint returns when a refresh token has
# been revoked or has expired.
REAL_INVALID_GRANT = RefreshError(
    "invalid_grant: Token has been expired or revoked.",
    {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "credential-health.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def alice(db_path) -> int:
    with connect_closing(db_path) as conn:
        return owner_id(conn)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_invalid_grant_classifies_as_reauth_required():
    state, category, detail = classify_credential_error(REAL_INVALID_GRANT)
    assert state == CredentialState.REAUTH_REQUIRED
    assert category == "invalid_grant"
    # The detail must be safe to show in the UI/an alert, but it is never
    # the raw exception text - it is built fresh from the classified code.
    assert "invalid_grant" in detail


@pytest.mark.parametrize("code", ["unauthorized_client", "invalid_client", "access_denied", "deleted_client"])
def test_other_permanent_google_error_codes_also_require_reauth(code):
    exc = RefreshError(f"{code}: rejected", {"error": code})
    state, category, _ = classify_credential_error(exc)
    assert state == CredentialState.REAUTH_REQUIRED
    assert category == code


@pytest.mark.parametrize("code", ["server_error", "temporarily_unavailable", "internal_failure"])
def test_transient_google_error_codes_do_not_demand_reauth(code):
    exc = RefreshError(f"{code}: try later", {"error": code})
    state, category, _ = classify_credential_error(exc)
    assert state == CredentialState.DEGRADED
    assert category == code


def test_an_unrecognised_refresh_error_is_conservative_not_alarmist():
    """A RefreshError this classifier has never seen before must not be
    treated as a confirmed dead grant - only a positively-identified
    permanent error code earns REAUTH_REQUIRED."""
    exc = RefreshError("some_future_error_code: who knows", {"error": "some_future_error_code"})
    state, _, _ = classify_credential_error(exc)
    assert state == CredentialState.DEGRADED


def test_a_transport_error_is_transient():
    state, category, _ = classify_credential_error(TransportError("DNS lookup failed"))
    assert state == CredentialState.DEGRADED
    assert category == "transport_error"


def test_a_missing_encryption_key_is_a_config_error_not_reauth_required():
    state, category, _ = classify_credential_error(crypto.CredentialKeyMissing("no key"))
    assert state == CredentialState.CONFIG_ERROR
    assert category == "credential_key_missing"


def test_classification_never_echoes_the_exceptions_own_message_verbatim():
    """The classifier only ever inspects exception *type* and, for a
    RefreshError, Google's own short `error` code - never the free-text
    message, which is exactly where a library could someday leak request
    detail. Poison the message with something distinctive and confirm it
    never survives into the classified output."""
    marker = "totally-distinctive-marker-should-never-survive-classification"
    poisoned = RefreshError(f"invalid_grant: {marker}", {"error": "invalid_grant"})
    _, _, detail = classify_credential_error(poisoned)
    assert marker not in detail


# ---------------------------------------------------------------------------
# Persistence / state machine
# ---------------------------------------------------------------------------


def test_a_fresh_success_is_healthy_with_no_failure_streak(db_path, alice):
    with connect_closing(db_path) as conn:
        credential_health.record_success(conn, user_id=alice, service="classroom")
        row = repo.get_credential_health(conn, user_id=alice, service="classroom")
    assert row["state"] == "healthy"
    assert row["last_success_at"] is not None
    assert row["first_failure_at"] is None


def test_a_failure_streak_records_when_it_began_and_holds_it(db_path, alice):
    with connect_closing(db_path) as conn:
        first = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        second = first + timedelta(minutes=15)
        credential_health.record_failure(
            conn, user_id=alice, service="classroom", exc=REAL_INVALID_GRANT, now=first.isoformat()
        )
        credential_health.record_failure(
            conn, user_id=alice, service="classroom", exc=REAL_INVALID_GRANT, now=second.isoformat()
        )
        row = repo.get_credential_health(conn, user_id=alice, service="classroom")

    assert row["state"] == "reauth_required"
    assert row["first_failure_at"] == first.isoformat()  # unmoved across the streak
    assert row["last_attempt_at"] == second.isoformat()  # advances every attempt


def test_a_success_after_a_failure_streak_clears_it(db_path, alice):
    with connect_closing(db_path) as conn:
        credential_health.record_failure(conn, user_id=alice, service="classroom", exc=REAL_INVALID_GRANT)
        credential_health.record_success(conn, user_id=alice, service="classroom")
        row = repo.get_credential_health(conn, user_id=alice, service="classroom")

    assert row["state"] == "healthy"
    assert row["first_failure_at"] is None


def test_last_success_at_survives_a_later_failure(db_path, alice):
    """So "how stale is my data" is always answerable mid-outage, not
    wiped the moment the outage starts."""
    with connect_closing(db_path) as conn:
        credential_health.record_success(conn, user_id=alice, service="classroom", now="2026-01-15T09:00:00+00:00")
        credential_health.record_failure(
            conn, user_id=alice, service="classroom", exc=REAL_INVALID_GRANT, now="2026-01-16T09:00:00+00:00"
        )
        row = repo.get_credential_health(conn, user_id=alice, service="classroom")

    assert row["state"] == "reauth_required"
    assert row["last_success_at"] == "2026-01-15T09:00:00+00:00"


def test_not_connected_never_claims_the_credential_once_worked(db_path, alice):
    """M1. NOT_CONNECTED means there is no credential at all, so there is
    nothing that can have "last worked" - recording it as a success made
    the account page render "Last worked: <now>" beneath a "not connected"
    badge for a service that has never been connected."""
    with connect_closing(db_path) as conn:
        credential_health.record_not_connected(conn, user_id=alice, service="calendar")
        row = repo.get_credential_health(conn, user_id=alice, service="calendar")

    assert row["state"] == "not_connected"
    assert row["last_success_at"] is None
    assert row["last_attempt_at"] is not None  # still observable that we looked
    assert row["first_failure_at"] is None     # and still not a failure


def test_not_connected_preserves_an_earlier_genuine_success(db_path, alice):
    """A service that worked and was later disconnected keeps its real
    "last worked" timestamp rather than having it erased or overwritten."""
    with connect_closing(db_path) as conn:
        credential_health.record_success(conn, user_id=alice, service="calendar", now="2026-01-15T09:00:00+00:00")
        credential_health.record_not_connected(conn, user_id=alice, service="calendar")
        row = repo.get_credential_health(conn, user_id=alice, service="calendar")

    assert row["state"] == "not_connected"
    assert row["last_success_at"] == "2026-01-15T09:00:00+00:00"


def test_not_connected_ends_a_failure_streak(db_path, alice):
    with connect_closing(db_path) as conn:
        credential_health.record_failure(conn, user_id=alice, service="calendar", exc=REAL_INVALID_GRANT)
        credential_health.record_not_connected(conn, user_id=alice, service="calendar")
        row = repo.get_credential_health(conn, user_id=alice, service="calendar")

    assert row["first_failure_at"] is None


def test_an_unknown_outcome_is_rejected_rather_than_guessed(db_path, alice):
    with connect_closing(db_path) as conn:
        with pytest.raises(ValueError):
            repo.record_credential_attempt(
                conn, user_id=alice, service="calendar", state="healthy", outcome="probably-fine",
            )


def test_classroom_and_calendar_health_are_independent(db_path, alice):
    """A dead Classroom credential must never be reported for Calendar and
    vice versa - the same isolation discipline as every other user-owned
    row, just between two services on one account."""
    with connect_closing(db_path) as conn:
        credential_health.record_failure(conn, user_id=alice, service="classroom", exc=REAL_INVALID_GRANT)
        credential_health.record_success(conn, user_id=alice, service="calendar")
        classroom = repo.get_credential_health(conn, user_id=alice, service="classroom")
        calendar = repo.get_credential_health(conn, user_id=alice, service="calendar")

    assert classroom["state"] == "reauth_required"
    assert calendar["state"] == "healthy"


def test_state_survives_a_process_restart(db_path, alice):
    """No in-memory cache anywhere - a fresh connection (standing in for a
    fresh process) must see exactly what was last persisted."""
    with connect_closing(db_path) as conn:
        credential_health.record_failure(conn, user_id=alice, service="classroom", exc=REAL_INVALID_GRANT)

    with connect_closing(db_path) as conn:  # a new connection/process
        row = repo.get_credential_health(conn, user_id=alice, service="classroom")
    assert row["state"] == "reauth_required"


def test_no_marker_string_ever_lands_in_a_credential_health_row(db_path, alice):
    marker = "distinctive-value-that-must-never-appear-in-a-stored-row"
    poisoned = RefreshError(f"invalid_grant: {marker}", {"error": "invalid_grant"})
    with connect_closing(db_path) as conn:
        credential_health.record_failure(conn, user_id=alice, service="classroom", exc=poisoned)
        row = repo.get_credential_health(conn, user_id=alice, service="classroom")

    row_text = " ".join(str(v) for v in dict(row).values())
    assert marker not in row_text


# ---------------------------------------------------------------------------
# A revoked refresh token, reproduced end to end through the real code
# path a live tick actually runs - not just the classifier in isolation.
# ---------------------------------------------------------------------------


def _fake_config(tmp_path, *, classroom_paths) -> Config:
    from ragra.adapters.calendar import default_calendar_token_path

    return Config(
        ragra_home=tmp_path, db_path=tmp_path / "unused.db", hermes_bin=None, notify_target=None,
        fast_student_id=None, calendar_id="primary",
        calendar_paths=type("P", (), {"oauth_client_file": classroom_paths.credentials_file,
                                        "token_file": default_calendar_token_path(tmp_path)})(),
        classroom_paths=classroom_paths, web_host="127.0.0.1", web_port=8731,
        sheets_api_key=None, fast_timetable_spreadsheet_id=None,
        smtp_host=None, smtp_port=0, smtp_username=None, smtp_password=None,
        smtp_use_ssl=False, email_from=None, email_to=None, web_base_url=None,
    )


def _write_live_token(path, scopes) -> None:
    """A stored credential that is currently valid (not expired), so the
    load path returns without attempting a refresh."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(
        json.dumps({
            "token": "placeholder-access-value-not-real",
            "refresh_token": "placeholder-refresh-value-not-real",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "placeholder-client-id-not-real",
            "client_secret": "placeholder-client-secret-not-real",
            "scopes": list(scopes),
            "expiry": future,
        }),
        encoding="utf-8",
    )


def test_the_legacy_path_hands_the_client_a_credential_not_another_client(
    tmp_path, db_path, alice, monkeypatch
):
    """Regression: the shared-resolver refactor passed
    `get_classroom_client`'s *client* into `ClassroomGoogleClient(...)`,
    building a client around another client instead of around a
    credential. No test covered a *successful* legacy load, so the whole
    suite stayed green while the owner's Classroom sync would have broken
    on the next successful reauthorization.
    """
    from google.oauth2.credentials import Credentials

    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomTokenPaths

    token_file = tmp_path / "token_classroom.json"
    _write_live_token(token_file, CLASSROOM_SCOPES)
    paths = ClassroomTokenPaths(
        credentials_file=tmp_path / "credentials.json",
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
    )
    config = _fake_config(tmp_path, classroom_paths=paths)

    seen = {}

    def _fake_build(serviceName, version, credentials=None, **kwargs):
        seen["credentials"] = credentials
        return object()

    monkeypatch.setattr("googleapiclient.discovery.build", _fake_build)

    with connect_closing(db_path) as conn:
        classroom_client_for(conn, config, user_id=alice)

    assert isinstance(seen["credentials"], Credentials), (
        "the Google client must be built around a credential, never around another client"
    )


def test_a_working_legacy_credential_is_adopted_into_the_encrypted_store(
    tmp_path, db_path, alice, monkeypatch
):
    """L5. After the first successful use there is exactly one live copy:
    the encrypted store (which is consulted first). Without this the file
    and the store are two independent copies and only one stays current as
    tokens rotate."""
    from ragra import crypto
    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomTokenPaths

    monkeypatch.setenv(crypto.KEY_ENV_VAR, crypto.generate_key())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())

    token_file = tmp_path / "token_classroom.json"
    _write_live_token(token_file, CLASSROOM_SCOPES)
    paths = ClassroomTokenPaths(
        credentials_file=tmp_path / "credentials.json",
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
    )
    config = _fake_config(tmp_path, classroom_paths=paths)

    with connect_closing(db_path) as conn:
        assert not google_credentials.has_credentials(
            conn, user_id=alice, service=google_credentials.CLASSROOM
        )
        classroom_client_for(conn, config, user_id=alice)
        assert google_credentials.has_credentials(
            conn, user_id=alice, service=google_credentials.CLASSROOM
        )
        assert repo.get_credential_health(conn, user_id=alice, service="classroom")["state"] == "healthy"


def test_adoption_is_skipped_without_a_key_rather_than_breaking_a_working_sync(
    tmp_path, db_path, alice, monkeypatch
):
    """A deployment with no encryption key is supported: adoption is an
    optimisation of where the credential lives, never a precondition for
    using one that already works."""
    from ragra import crypto
    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomTokenPaths

    monkeypatch.delenv(crypto.KEY_ENV_VAR, raising=False)
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())

    token_file = tmp_path / "token_classroom.json"
    _write_live_token(token_file, CLASSROOM_SCOPES)
    paths = ClassroomTokenPaths(
        credentials_file=tmp_path / "credentials.json",
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
    )
    config = _fake_config(tmp_path, classroom_paths=paths)

    with connect_closing(db_path) as conn:
        classroom_client_for(conn, config, user_id=alice)  # must not raise
        assert not google_credentials.has_credentials(
            conn, user_id=alice, service=google_credentials.CLASSROOM
        )
        assert repo.get_credential_health(conn, user_id=alice, service="classroom")["state"] == "healthy"


def _rotating_refresh(new_refresh_token: str):
    """Stand in for Google rotating the refresh token during a refresh.

    Google may return a *replacement* refresh token on a refresh grant.
    Dropping it turns an account that works today into one that silently
    needs re-authorization later, so the replacement must be persisted,
    not just used in memory."""

    def _refresh(self, request):
        self.token = "placeholder-new-access-value"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        self._refresh_token = new_refresh_token

    return _refresh


def test_a_rotated_refresh_token_is_persisted_to_the_encrypted_store(
    tmp_path, db_path, alice, monkeypatch
):
    """Rotation persistence, encrypted path."""
    from google.oauth2.credentials import Credentials

    from ragra import crypto
    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomTokenPaths

    monkeypatch.setenv(crypto.KEY_ENV_VAR, crypto.generate_key())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())
    monkeypatch.setattr(Credentials, "refresh", _rotating_refresh("placeholder-rotated-value"))

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps({
        "token": "placeholder-old-access-value",
        "refresh_token": "placeholder-original-value",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "placeholder-client-id-not-real",
        "client_secret": "placeholder-client-secret-not-real",
        "scopes": list(CLASSROOM_SCOPES),
        "expiry": past,
    })

    paths = ClassroomTokenPaths(
        credentials_file=tmp_path / "credentials.json",
        token_file=tmp_path / "token_classroom.json",
        legacy_token_file=tmp_path / "token.json",
    )
    config = _fake_config(tmp_path, classroom_paths=paths)

    with connect_closing(db_path) as conn:
        google_credentials.store(
            conn, user_id=alice, service=google_credentials.CLASSROOM, payload=payload
        )
        classroom_client_for(conn, config, user_id=alice)
        stored = google_credentials.load(
            conn, user_id=alice, service=google_credentials.CLASSROOM
        )
        health = repo.get_credential_health(conn, user_id=alice, service="classroom")

    assert "placeholder-rotated-value" in stored, "the replacement refresh token was not persisted"
    assert "placeholder-original-value" not in stored, "the superseded refresh token was left in place"
    assert health["state"] == "healthy"


def test_a_rotated_refresh_token_is_persisted_on_the_legacy_file_path(
    tmp_path, db_path, alice, monkeypatch
):
    """Rotation persistence, legacy path - the one the owner's account is
    actually on until the first successful use adopts it."""
    from google.oauth2.credentials import Credentials

    from ragra import crypto
    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomTokenPaths

    monkeypatch.delenv(crypto.KEY_ENV_VAR, raising=False)  # legacy-only deployment
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())
    monkeypatch.setattr(Credentials, "refresh", _rotating_refresh("placeholder-rotated-value"))

    token_file = tmp_path / "token_classroom.json"
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    token_file.write_text(
        json.dumps({
            "token": "placeholder-old-access-value",
            "refresh_token": "placeholder-original-value",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "placeholder-client-id-not-real",
            "client_secret": "placeholder-client-secret-not-real",
            "scopes": list(CLASSROOM_SCOPES),
            "expiry": past,
        }),
        encoding="utf-8",
    )
    paths = ClassroomTokenPaths(
        credentials_file=tmp_path / "credentials.json",
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
    )
    config = _fake_config(tmp_path, classroom_paths=paths)

    with connect_closing(db_path) as conn:
        classroom_client_for(conn, config, user_id=alice)

    written = token_file.read_text(encoding="utf-8")
    assert "placeholder-rotated-value" in written
    assert "placeholder-original-value" not in written


def test_a_stored_credential_survives_a_restart_without_reauthorization(
    tmp_path, db_path, alice, monkeypatch
):
    """The whole point of offline access: a fresh process reuses the
    stored credential and never reaches an interactive flow."""
    from ragra import crypto
    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomTokenPaths

    monkeypatch.setenv(crypto.KEY_ENV_VAR, crypto.generate_key())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps({
        "token": "placeholder-access-value",
        "refresh_token": "placeholder-refresh-value",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "placeholder-client-id-not-real",
        "client_secret": "placeholder-client-secret-not-real",
        "scopes": list(CLASSROOM_SCOPES),
        "expiry": future,
    })
    paths = ClassroomTokenPaths(
        credentials_file=tmp_path / "credentials.json",
        token_file=tmp_path / "token_classroom.json",
        legacy_token_file=tmp_path / "token.json",
    )
    config = _fake_config(tmp_path, classroom_paths=paths)

    with connect_closing(db_path) as conn:
        google_credentials.store(
            conn, user_id=alice, service=google_credentials.CLASSROOM, payload=payload
        )

    # Three "restarts": each opens a brand-new connection, as a new process
    # would, and must succeed without any interactive flow being reachable.
    for _ in range(3):
        with connect_closing(db_path) as conn:
            classroom_client_for(conn, config, user_id=alice)
            assert repo.get_credential_health(
                conn, user_id=alice, service="classroom"
            )["state"] == "healthy"


def test_a_rejected_refresh_token_is_not_silent(tmp_path, db_path, alice, monkeypatch):
    """Google refresh endpoint -> invalid_grant -> Ragra must NOT silently
    appear healthy.

    Exercises classroom_client_for (the real seam a live tick calls) with
    a stored credential that has a refresh_token and is expired - the
    shape a token file has once Google has revoked the grant behind it -
    with Credentials.refresh raising the error Google returns in that
    case.
    """
    from google.oauth2.credentials import Credentials

    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomTokenPaths

    token_file = tmp_path / "token_classroom.json"
    past_expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    token_file.write_text(
        json.dumps({
            "token": "placeholder-access-value-not-real",
            "refresh_token": "placeholder-refresh-value-not-real",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "placeholder-client-id-not-real",
            "client_secret": "placeholder-client-secret-not-real",
            "scopes": list(CLASSROOM_SCOPES),
            "expiry": past_expiry,
        }),
        encoding="utf-8",
    )
    paths = ClassroomTokenPaths(
        credentials_file=tmp_path / "credentials.json",
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
    )
    config = _fake_config(tmp_path, classroom_paths=paths)

    def _raise_invalid_grant(self, request):
        raise REAL_INVALID_GRANT

    monkeypatch.setattr(Credentials, "refresh", _raise_invalid_grant)

    with connect_closing(db_path) as conn:
        with pytest.raises(UserCredentialsUnavailable):
            classroom_client_for(conn, config, user_id=alice)

        row = repo.get_credential_health(conn, user_id=alice, service=google_credentials.CLASSROOM)

    # The assertion that actually matters: not "an exception
    # was raised" (the old code already did that) but "the system now
    # durably knows, and will keep saying, that this credential is dead" -
    # not DEGRADED (which would imply it might fix itself), not silently
    # HEALTHY, and specifically not swallowed into an opaque generic string.
    assert row["state"] == "reauth_required"
    assert row["last_error_category"] == "invalid_grant"
    assert "placeholder-refresh-value-not-real" not in (row["last_error_detail"] or "")
    assert "placeholder-client-secret-not-real" not in (row["last_error_detail"] or "")
