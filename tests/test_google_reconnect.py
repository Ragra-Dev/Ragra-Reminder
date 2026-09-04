"""The "Reconnect Google" recovery flow: the low-friction alternative to
"open a terminal and run ragra classroom-auth" this incident's fix adds.

Exercised end to end through the real routes with a fake data-authorization
provider, the same posture as tests/test_auth_sign_in.py takes for sign-in:
only the call to Google is replaced, everything else (state/PKCE handling,
CSRF, credential storage, health-state transition) is the production code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ragra import crypto
from ragra.adapters import credential_health, google_credentials
from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.web import google_reconnect
from ragra.web.app import create_app
from tests.support import make_user, owner_id, sign_in


class FakeDataProvider:
    """Stands in for Google. Records what it was asked and returns
    whichever credentials-like object the test wants the exchange to
    yield."""

    def __init__(self):
        self.authorization_calls: list[dict] = []
        self.exchanges: list[dict] = []
        self.fail_with: Exception | None = None
        self.authorization_fails_with: Exception | None = None
        self.omit_refresh_token = False
        self.granted_scopes: tuple[str, ...] | None = None  # None = grant exactly what was asked for

    def authorization_url(self, *, service: str, state: str, code_challenge: str) -> str:
        if self.authorization_fails_with is not None:
            raise self.authorization_fails_with
        self.authorization_calls.append({"service": service, "state": state, "code_challenge": code_challenge})
        return f"https://accounts.example/authorize?service={service}&state={state}"

    def exchange_code(self, *, service: str, code: str, code_verifier: str):
        self.exchanges.append({"service": service, "code": code, "code_verifier": code_verifier})
        if self.fail_with is not None:
            raise self.fail_with
        granted = (
            self.granted_scopes
            if self.granted_scopes is not None
            else google_reconnect.required_scopes(service)
        )
        return _FakeCredentials(omit_refresh_token=self.omit_refresh_token, scopes=granted)


class _FakeCredentials:
    def __init__(self, *, omit_refresh_token: bool = False, scopes=()):
        self._omit = omit_refresh_token
        self.refresh_token = None if omit_refresh_token else "placeholder-refresh-value"
        self.scopes = list(scopes)

    def to_json(self) -> str:
        import json

        payload = {"token": "placeholder-access-value", "scopes": self.scopes}
        if not self._omit:
            payload["refresh_token"] = self.refresh_token
        return json.dumps(payload)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "reconnect.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def key(monkeypatch):
    generated = crypto.generate_key()
    monkeypatch.setenv(crypto.KEY_ENV_VAR, generated)
    return crypto.load_key()


@pytest.fixture
def provider():
    return FakeDataProvider()


@pytest.fixture
def client(db_path, provider, key):
    c = TestClient(create_app(db_path, data_auth_provider=provider), follow_redirects=False)
    sign_in(c, db_path)
    return c


@pytest.fixture
def alice(db_path) -> int:
    with connect_closing(db_path) as conn:
        return owner_id(conn)


# ---------------------------------------------------------------------------
# repo/module-level state handling
# ---------------------------------------------------------------------------


def test_begin_reconnect_returns_the_providers_url_and_persists_state(db_path, alice, provider):
    with connect_closing(db_path) as conn:
        url = google_reconnect.begin_reconnect(
            conn, provider, user_id=alice, service="classroom", now=datetime.now(timezone.utc)
        )
    assert url.startswith("https://accounts.example/authorize?service=classroom")
    assert len(provider.authorization_calls) == 1


def test_consume_reconnect_is_single_use(db_path, alice, provider):
    with connect_closing(db_path) as conn:
        now = datetime.now(timezone.utc)
        url = google_reconnect.begin_reconnect(conn, provider, user_id=alice, service="classroom", now=now)
        state = url.split("state=")[1]

        consumed = google_reconnect.consume_reconnect(conn, state=state, now=now)
        assert consumed.user_id == alice
        assert consumed.service == "classroom"

        with pytest.raises(google_reconnect.ReconnectError):
            google_reconnect.consume_reconnect(conn, state=state, now=now)


def test_consume_reconnect_rejects_an_expired_attempt(db_path, alice, provider):
    with connect_closing(db_path) as conn:
        started = datetime.now(timezone.utc)
        url = google_reconnect.begin_reconnect(conn, provider, user_id=alice, service="calendar", now=started)
        state = url.split("state=")[1]

        long_after = started + timedelta(minutes=20)
        with pytest.raises(google_reconnect.ReconnectError):
            google_reconnect.consume_reconnect(conn, state=state, now=long_after)


def test_consume_reconnect_rejects_an_unknown_state(db_path, alice):
    with connect_closing(db_path) as conn:
        with pytest.raises(google_reconnect.ReconnectError):
            google_reconnect.consume_reconnect(conn, state="not-a-real-state", now=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# End to end through the real routes
# ---------------------------------------------------------------------------


def test_the_full_reconnect_round_trip_restores_healthy_state(client, db_path, alice, provider):
    with connect_closing(db_path) as conn:
        credential_health.record_failure(
            conn, user_id=alice, service="classroom",
            exc=RuntimeError("stand-in for the earlier dead credential"),
        )

    begin = client.post("/account/google/reconnect/classroom")
    assert begin.status_code == 303
    redirect_url = begin.headers["location"]
    assert "accounts.example/authorize" in redirect_url
    state = redirect_url.split("state=")[1]

    callback = client.get(f"/account/google/callback?code=real-code&state={state}")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/account"

    with connect_closing(db_path) as conn:
        row = repo.get_credential_health(conn, user_id=alice, service="classroom")
        stored = google_credentials.load(conn, user_id=alice, service=google_credentials.CLASSROOM)

    assert row["state"] == "healthy"
    assert "placeholder-refresh-value" in stored  # the encrypted store really holds the new credential


def test_reconnect_requires_csrf(db_path, provider, key):
    from ragra.web import sessions

    c = TestClient(create_app(db_path, data_auth_provider=provider), follow_redirects=False)
    with connect_closing(db_path) as conn:
        user_id = owner_id(conn)
        token = sessions.create_session(conn, user_id=user_id, now=datetime.now(timezone.utc))
    c.cookies.set(sessions.COOKIE_NAME, token)
    # Deliberately no CSRF header set.

    response = c.post("/account/google/reconnect/classroom")
    assert response.status_code == 403


def test_reconnect_rejects_an_unknown_service(client):
    response = client.post("/account/google/reconnect/gmail")
    assert response.status_code == 404


def test_a_provider_that_returns_no_refresh_token_is_rejected(client, db_path, alice, provider):
    """A credential the unattended tick can't actually use later must never
    be accepted as if reconnecting succeeded."""
    provider.omit_refresh_token = True

    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]

    callback = client.get(f"/account/google/callback?code=real-code&state={state}")
    assert callback.status_code == 400

    with connect_closing(db_path) as conn:
        assert not google_credentials.has_credentials(conn, user_id=alice, service=google_credentials.CLASSROOM)
        # Nothing was stored, so there is nothing whose health changed:
        # a failed *attempt* leaves stored-credential health alone (M2).
        assert repo.get_credential_health(conn, user_id=alice, service="classroom") is None


def test_a_provider_exchange_failure_does_not_store_a_partial_credential(client, db_path, alice, provider):
    provider.fail_with = google_reconnect.ReconnectError("simulated Google-side rejection")

    begin = client.post("/account/google/reconnect/calendar")
    state = begin.headers["location"].split("state=")[1]

    callback = client.get(f"/account/google/callback?code=real-code&state={state}")
    assert callback.status_code == 400

    with connect_closing(db_path) as conn:
        assert not google_credentials.has_credentials(conn, user_id=alice, service=google_credentials.CALENDAR)


def test_a_replayed_callback_url_is_rejected(client):
    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]

    first = client.get(f"/account/google/callback?code=real-code&state={state}")
    assert first.status_code == 303

    second = client.get(f"/account/google/callback?code=real-code&state={state}")
    assert second.status_code == 400


def test_the_callback_page_never_prints_a_credential_value(client):
    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    callback = client.get(f"/account/google/callback?code=real-code&state={state}")
    assert "placeholder-refresh-value" not in callback.text
    assert "placeholder-access-value" not in callback.text


def test_account_page_shows_a_reconnect_action_for_each_service(client):
    body = client.get("/account").text
    assert "/account/google/reconnect/classroom" in body
    assert "/account/google/reconnect/calendar" in body


# ---------------------------------------------------------------------------
# Security-review regressions (M2, M3, M4, L2, L4)
# ---------------------------------------------------------------------------


def test_cancelling_at_googles_consent_screen_does_not_degrade_a_healthy_credential(
    client, db_path, alice
):
    """M2. A reconnect *attempt* says nothing about the credential already
    in storage, so abandoning one must leave its health exactly as it
    was - previously this flipped a working integration to `degraded`
    until the next tick happened to correct it."""
    with connect_closing(db_path) as conn:
        credential_health.record_success(conn, user_id=alice, service="classroom")
        before = dict(repo.get_credential_health(conn, user_id=alice, service="classroom"))

    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    denied = client.get(f"/account/google/callback?error=access_denied&state={state}")
    assert denied.status_code == 400

    with connect_closing(db_path) as conn:
        after = dict(repo.get_credential_health(conn, user_id=alice, service="classroom"))

    assert after == before, "an abandoned consent flow must not touch stored-credential health"
    assert after["state"] == "healthy"


@pytest.mark.parametrize(
    "boom",
    [
        ConnectionError("network unreachable"),
        TimeoutError("timed out"),
        ValueError("malformed JSON from the token endpoint"),
        KeyError("client_id"),
        FileNotFoundError(2, "No such file or directory", r"C:\Users\someone\secret-location\credentials.json"),
    ],
    ids=["network", "timeout", "bad-json", "key-error", "missing-file"],
)
def test_provider_failures_produce_a_clean_400_without_leaking_internals(client, provider, boom):
    """M3. The real provider can raise network, JSON, KeyError and
    filesystem errors, none of which were caught before - each produced a
    500, and the filesystem one carried a local path into the response."""
    provider.fail_with = boom

    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    response = client.get(f"/account/google/callback?code=real-code&state={state}")

    assert response.status_code == 400
    body = response.text
    assert "secret-location" not in body
    assert "credentials.json" not in body
    assert "Traceback" not in body
    assert "network unreachable" not in body


def test_a_misconfigured_deployment_reports_503_without_naming_a_local_path(client, provider):
    """M3/L4. A missing or unusable client registration is a deployment
    problem, answered as one - and never by echoing the path."""
    provider.fail_with = google_reconnect.ReconnectNotConfigured(
        r"no such file: C:\Users\someone\hidden\credentials.json"
    )

    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    response = client.get(f"/account/google/callback?code=real-code&state={state}")

    assert response.status_code == 503
    assert "hidden" not in response.text
    assert "credentials.json" not in response.text


def test_a_grant_missing_required_scopes_is_refused_before_anything_is_stored(
    client, db_path, alice, provider
):
    """M4. A partial grant (the user unchecks a scope on the consent
    screen) must be refused while they are still there to fix it, never
    stored and marked HEALTHY to fail on a later unattended tick."""
    full = google_reconnect.required_scopes("classroom")
    provider.granted_scopes = full[:-1]  # everything except one

    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    response = client.get(f"/account/google/callback?code=real-code&state={state}")

    assert response.status_code == 400
    assert full[-1] in response.text  # names what is missing, so it is actionable
    with connect_closing(db_path) as conn:
        assert not google_credentials.has_credentials(
            conn, user_id=alice, service=google_credentials.CLASSROOM
        )
        assert repo.get_credential_health(conn, user_id=alice, service="classroom") is None


def test_a_grant_for_the_other_service_cannot_be_filed_under_this_one(
    client, db_path, alice, provider
):
    """M4. The two scope sets are disjoint, so a Calendar grant returned
    during a Classroom reconnect must be rejected outright rather than
    stored as `classroom` and left to fail as a confusing 403 later."""
    provider.granted_scopes = google_reconnect.required_scopes("calendar")

    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    response = client.get(f"/account/google/callback?code=real-code&state={state}")

    assert response.status_code == 400
    with connect_closing(db_path) as conn:
        assert not google_credentials.has_credentials(
            conn, user_id=alice, service=google_credentials.CLASSROOM
        )


def test_extra_granted_scopes_are_acceptable(client, db_path, alice, provider):
    """The check is "sufficient", not "exactly equal" - Google returning
    more than was asked for must not block a legitimate reconnect."""
    provider.granted_scopes = google_reconnect.required_scopes("classroom") + ("https://example/extra",)

    begin = client.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    assert client.get(f"/account/google/callback?code=real-code&state={state}").status_code == 303

    with connect_closing(db_path) as conn:
        assert repo.get_credential_health(conn, user_id=alice, service="classroom")["state"] == "healthy"


def test_a_failing_authorization_url_leaves_no_unusable_state_row(client, db_path, provider):
    """L2. The row is written only once the provider has produced a URL,
    so a deployment that cannot start a reconnect does not accumulate
    attempts nobody can ever complete."""
    provider.authorization_fails_with = google_reconnect.ReconnectNotConfigured("not configured")

    response = client.post("/account/google/reconnect/classroom")
    assert response.status_code == 503

    with connect_closing(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM google_reconnect_states").fetchone()["c"]
    assert rows == 0


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://127.0.0.1:8731/account/google/callback",
        "http://localhost:8731/account/google/callback",
        "http://[::1]:8731/account/google/callback",
    ],
)
def test_an_installed_client_accepts_loopback_redirects(redirect_uri):
    """L4."""
    google_reconnect.assert_redirect_uri_allowed(client_type="installed", redirect_uri=redirect_uri)


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://ragra.example.com/account/google/callback",  # hosted origin
        "http://0.0.0.0:8731/account/google/callback",        # bound to every interface
        "http://192.168.1.50:8731/account/google/callback",   # LAN address
    ],
)
def test_an_installed_client_refuses_a_non_loopback_redirect(redirect_uri):
    """L4. A Desktop client is a *public* client - its secret is not
    secret - so it must never be used against an address other machines
    can reach. Refused here rather than discovered in Phase 4."""
    with pytest.raises(google_reconnect.ReconnectNotConfigured):
        google_reconnect.assert_redirect_uri_allowed(client_type="installed", redirect_uri=redirect_uri)


def test_a_web_client_requires_https():
    """L4. The counterpart rule for the client type a hosted deployment
    would actually use."""
    google_reconnect.assert_redirect_uri_allowed(
        client_type="web", redirect_uri="https://ragra.example.com/account/google/callback"
    )
    with pytest.raises(google_reconnect.ReconnectNotConfigured):
        google_reconnect.assert_redirect_uri_allowed(
            client_type="web", redirect_uri="http://ragra.example.com/account/google/callback"
        )


def test_reconnecting_without_an_encryption_key_fails_loud_not_silent(db_path, provider, monkeypatch):
    """No key configured (RAGRA_CREDENTIAL_KEY unset) must produce a clear
    503 and never a silently-accepted plaintext credential."""
    monkeypatch.delenv(crypto.KEY_ENV_VAR, raising=False)
    c = TestClient(create_app(db_path, data_auth_provider=provider), follow_redirects=False)
    sign_in(c, db_path)

    begin = c.post("/account/google/reconnect/classroom")
    state = begin.headers["location"].split("state=")[1]
    callback = c.get(f"/account/google/callback?code=real-code&state={state}")

    assert callback.status_code == 503
    assert "RAGRA_CREDENTIAL_KEY" in callback.text  # actionable for an operator
    with connect_closing(db_path) as conn:
        alice = owner_id(conn)
        assert not google_credentials.has_credentials(conn, user_id=alice, service=google_credentials.CLASSROOM)
