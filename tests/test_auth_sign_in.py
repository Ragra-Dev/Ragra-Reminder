"""Google sign-in: the security properties, not the happy path.

Every test here is written as an attack or a mistake rather than as a
demonstration that logging in works. The flow is exercised end to end
through the real routes with a fake identity provider, so the state
handling, PKCE, allow-list, adoption rule, session issuance and cookie
flags are all the production code paths - only the call to Google is
replaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.web import auth, csrf, sessions
from ragra.web.app import create_app

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

OWNER = auth.GoogleIdentity(
    subject="google-sub-owner",
    email="owner@example.com",
    email_verified=True,
    display_name="The Owner",
)
STRANGER = auth.GoogleIdentity(
    subject="google-sub-stranger",
    email="stranger@example.com",
    email_verified=True,
    display_name="A Stranger",
)
COLLEAGUE = auth.GoogleIdentity(
    subject="google-sub-colleague",
    email="colleague@example.com",
    email_verified=True,
    display_name="A Colleague",
)


class FakeProvider:
    """Stands in for Google. Records what it was asked and returns whichever
    identity the test wants the exchange to yield."""

    def __init__(self, identity=OWNER):
        self.identity = identity
        self.authorization_calls: list[dict] = []
        self.exchanges: list[dict] = []
        self.fail_with: Exception | None = None

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        self.authorization_calls.append({"state": state, "code_challenge": code_challenge})
        return f"https://accounts.example/authorize?state={state}&cc={code_challenge}"

    def exchange_code(self, *, code: str, code_verifier: str):
        self.exchanges.append({"code": code, "code_verifier": code_verifier})
        if self.fail_with is not None:
            raise self.fail_with
        return self.identity


def _settings(**overrides) -> auth.AuthSettings:
    base = dict(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="http://127.0.0.1:8731/auth/callback",
        owner_email="owner@example.com",
        allowed_emails=frozenset(),
        secure_cookies=False,
    )
    base.update(overrides)
    return auth.AuthSettings(**base)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "auth.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def client(db_path, provider):
    app = create_app(db_path, auth_settings=_settings(), identity_provider=provider)
    return TestClient(app, follow_redirects=False)


def _state_from(provider: FakeProvider) -> str:
    return provider.authorization_calls[-1]["state"]


def _complete_sign_in(client, provider, *, identity=None, next_path=""):
    if identity is not None:
        provider.identity = identity
    client.get(f"/login?next={next_path}" if next_path else "/login")
    response = client.get(f"/auth/callback?code=auth-code&state={_state_from(provider)}")
    # A real browser gets this in every rendered form; setting it as a
    # default header is the test-client equivalent. Tests about CSRF itself
    # live in tests/test_csrf.py and deliberately do not use this path.
    token = client.cookies.get(sessions.COOKIE_NAME)
    if token:
        client.headers[csrf.HEADER_NAME] = csrf.token_for(token)
    return response


# ---------------------------------------------------------------------------
# Access control: what an unauthenticated request can reach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/missed", "/tasks", "/announcements", "/deliveries", "/brief"])
def test_pages_are_not_readable_without_signing_in(client, path):
    resp = client.get(path, headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


@pytest.mark.parametrize(
    "path", ["/tasks/1/complete", "/tasks/1/cancel", "/announcements/1/archive"]
)
def test_state_changing_posts_are_rejected_without_signing_in(client, path):
    """Refused outright, never redirected to sign in: bouncing a POST
    through a login page would silently discard what was submitted and let
    an unauthenticated request believe it had succeeded.

    The rejection is 403 rather than 401 because the CSRF check runs first
    and a request with no session cannot carry a valid CSRF token - the
    token is derived from the session. That ordering is deliberate: the
    cheaper, unconditional check runs before anything touches the
    database."""
    resp = client.post(path)
    assert resp.status_code == 403


def test_a_page_request_is_sent_to_sign_in_and_returns_to_where_it_started(client):
    resp = client.get("/missed", headers={"accept": "text/html"})
    assert resp.headers["location"] == "/login?next=/missed"


# ---------------------------------------------------------------------------
# The OAuth state parameter
# ---------------------------------------------------------------------------


def test_a_callback_with_no_state_is_rejected(client):
    assert client.get("/auth/callback?code=x").status_code == 400


def test_a_callback_with_a_forged_state_is_rejected(client, provider):
    """The CSRF defence for the callback: an attacker who can make the
    victim's browser hit /auth/callback cannot supply a state that matches
    an attempt the victim's browser actually started."""
    client.get("/login")
    resp = client.get("/auth/callback?code=x&state=state-i-made-up")
    assert resp.status_code == 400
    assert provider.exchanges == []


def test_a_state_cannot_be_used_twice(client, provider):
    """Replay defence. A captured callback URL - from history, a referrer
    header, a shared screenshot - must not sign anyone in a second time."""
    client.get("/login")
    state = _state_from(provider)

    assert client.get(f"/auth/callback?code=c1&state={state}").status_code == 303
    assert client.get(f"/auth/callback?code=c2&state={state}").status_code == 400
    assert len(provider.exchanges) == 1


def test_an_expired_state_is_rejected(db_path, provider):
    with connect_closing(db_path) as conn:
        auth.begin_sign_in(conn, provider, now=NOW)
        state = _state_from(provider)

        with pytest.raises(auth.AuthError):
            auth.consume_state(conn, state=state, now=NOW + auth.STATE_LIFETIME)


def test_a_failed_callback_still_spends_its_state(client, provider):
    """An error response from the provider must not leave a reusable
    attempt behind for someone else to complete."""
    client.get("/login")
    state = _state_from(provider)

    assert client.get(f"/auth/callback?error=access_denied&state={state}").status_code == 400
    assert client.get(f"/auth/callback?code=c&state={state}").status_code == 400


def test_expired_states_are_purgeable(db_path, provider):
    with connect_closing(db_path) as conn:
        auth.begin_sign_in(conn, provider, now=NOW)
        assert auth.purge_expired_states(conn, now=NOW) == 0
        assert auth.purge_expired_states(conn, now=NOW + auth.STATE_LIFETIME) == 1


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_the_authorization_request_carries_a_challenge_not_the_verifier(client, provider):
    client.get("/login")
    challenge = provider.authorization_calls[-1]["code_challenge"]

    assert challenge
    assert "=" not in challenge  # base64url, unpadded (RFC 7636 4.2)


def test_the_exchange_presents_the_verifier_that_matches_the_challenge(client, provider):
    client.get("/login")
    challenge = provider.authorization_calls[-1]["code_challenge"]

    client.get(f"/auth/callback?code=c&state={_state_from(provider)}")

    verifier = provider.exchanges[-1]["code_verifier"]
    assert auth.code_challenge_for(verifier) == challenge


def test_the_verifier_is_long_enough_to_be_unguessable(client, provider):
    client.get("/login")
    client.get(f"/auth/callback?code=c&state={_state_from(provider)}")
    verifier = provider.exchanges[-1]["code_verifier"]
    assert 43 <= len(verifier) <= 128  # RFC 7636 4.1


# ---------------------------------------------------------------------------
# Open redirect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "http://evil.example",
        "javascript:alert(1)",
        "\\\\evil.example",
        # These pass a naive check - one leading slash, no scheme, no
        # netloc - but browsers normalise the backslash, turning them into
        # protocol-relative URLs that navigate off-site.
        "/\\evil.example",
        "/\\/evil.example",
        "/missed\\@evil.example",
    ],
)
def test_a_hostile_post_login_destination_is_discarded(client, provider, hostile):
    """Sign-in must not become a redirector that lends Ragra's origin to
    somebody else's phishing page."""
    client.get("/login", params={"next": hostile})
    resp = client.get(f"/auth/callback?code=c&state={_state_from(provider)}")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_a_local_post_login_destination_is_honoured(client, provider):
    client.get("/login", params={"next": "/missed"})
    resp = client.get(f"/auth/callback?code=c&state={_state_from(provider)}")
    assert resp.headers["location"] == "/missed"


# ---------------------------------------------------------------------------
# Who is allowed in
# ---------------------------------------------------------------------------


def test_an_unlisted_account_is_refused(db_path, provider):
    """Closed by default. The interesting failure for a personal system is
    not "someone couldn't sign in", it is "someone signed in"."""
    provider.identity = STRANGER
    app = create_app(db_path, auth_settings=_settings(), identity_provider=provider)
    client = TestClient(app, follow_redirects=False)

    resp = _complete_sign_in(client, provider)

    assert resp.status_code == 403
    with connect_closing(db_path) as conn:
        assert len(repo.list_users(conn)) == 1  # no account was created


def test_nobody_is_admitted_when_no_owner_or_allow_list_is_configured(db_path, provider):
    app = create_app(
        db_path,
        auth_settings=_settings(owner_email=None, allowed_emails=frozenset()),
        identity_provider=provider,
    )
    client = TestClient(app, follow_redirects=False)

    assert _complete_sign_in(client, provider).status_code == 403


def test_an_unverified_email_is_never_matched_against_the_allow_list(db_path, provider):
    """An unverified address proves nothing about who controls it, so it
    must not be usable to claim the owner's identity."""
    provider.identity = auth.GoogleIdentity(
        subject="google-sub-impostor",
        email="owner@example.com",
        email_verified=False,
        display_name="Not The Owner",
    )
    app = create_app(db_path, auth_settings=_settings(), identity_provider=provider)
    client = TestClient(app, follow_redirects=False)

    assert _complete_sign_in(client, provider).status_code == 403


def test_an_allow_listed_colleague_gets_their_own_empty_account(db_path, provider):
    app = create_app(
        db_path,
        auth_settings=_settings(allowed_emails=frozenset({"colleague@example.com"})),
        identity_provider=provider,
    )
    client = TestClient(app, follow_redirects=False)

    assert _complete_sign_in(client, provider, identity=COLLEAGUE).status_code == 303

    with connect_closing(db_path) as conn:
        users = repo.list_users(conn)
        assert len(users) == 2
        colleague = repo.get_user_by_google_sub(conn, google_sub=COLLEAGUE.subject)
        # The pre-identity row is untouched: its history still belongs to
        # whoever claims it as owner, not to the first colleague to arrive.
        assert repo.unlinked_user_id(conn) is not None
        assert colleague["id"] != repo.unlinked_user_id(conn)


def test_email_matching_ignores_case_and_surrounding_space(db_path, provider):
    provider.identity = auth.GoogleIdentity(
        subject="google-sub-owner", email="  Owner@Example.COM  ",
        email_verified=True, display_name="The Owner",
    )
    app = create_app(db_path, auth_settings=_settings(), identity_provider=provider)
    client = TestClient(app, follow_redirects=False)

    assert _complete_sign_in(client, provider).status_code == 303


# ---------------------------------------------------------------------------
# Adopting the pre-identity owner
# ---------------------------------------------------------------------------


def test_the_owner_adopts_the_pre_identity_row_rather_than_getting_a_new_one(db_path, provider):
    """Without this, the first sign-in would strand every pre-P3 record
    behind an account nobody can reach."""
    with connect_closing(db_path) as conn:
        original = repo.unlinked_user_id(conn)

    app = create_app(db_path, auth_settings=_settings(), identity_provider=provider)
    client = TestClient(app, follow_redirects=False)
    _complete_sign_in(client, provider)

    with connect_closing(db_path) as conn:
        assert len(repo.list_users(conn)) == 1
        adopted = repo.get_user_by_google_sub(conn, google_sub=OWNER.subject)
        assert adopted["id"] == original
        assert repo.unlinked_user_id(conn) is None  # no longer claimable


def test_adoption_happens_at_most_once(db_path, provider):
    """A second Google account using the same address - after a transfer,
    say - must not be able to claim data that already has an owner."""
    app = create_app(db_path, auth_settings=_settings(), identity_provider=provider)
    client = TestClient(app, follow_redirects=False)
    _complete_sign_in(client, provider)

    second = auth.GoogleIdentity(
        subject="google-sub-different-account", email="owner@example.com",
        email_verified=True, display_name="Someone Else",
    )
    fresh = TestClient(
        create_app(db_path, auth_settings=_settings(), identity_provider=provider),
        follow_redirects=False,
    )
    _complete_sign_in(fresh, provider, identity=second)

    with connect_closing(db_path) as conn:
        original = repo.get_user_by_google_sub(conn, google_sub=OWNER.subject)
        newcomer = repo.get_user_by_google_sub(conn, google_sub=second.subject)
        assert original["id"] != newcomer["id"]


def test_identity_follows_the_subject_not_the_email(db_path, provider):
    """A Google account's email can change; its subject cannot. Matching on
    email would let a reassigned address inherit somebody else's data."""
    app = create_app(db_path, auth_settings=_settings(), identity_provider=provider)
    client = TestClient(app, follow_redirects=False)
    _complete_sign_in(client, provider)

    with connect_closing(db_path) as conn:
        user_id = repo.get_user_by_google_sub(conn, google_sub=OWNER.subject)["id"]

    renamed = auth.GoogleIdentity(
        subject=OWNER.subject, email="owner-new-address@example.com",
        email_verified=True, display_name="The Owner",
    )
    _complete_sign_in(client, provider, identity=renamed)

    with connect_closing(db_path) as conn:
        assert len(repo.list_users(conn)) == 1
        again = repo.get_user_by_google_sub(conn, google_sub=OWNER.subject)
        assert again["id"] == user_id
        assert again["email"] == "owner-new-address@example.com"


def test_an_already_linked_subject_is_admitted_even_if_the_allow_list_changed(db_path, provider):
    """Deliberate: revoking access is done by deleting the account or its
    sessions, not by editing an environment variable, so that an
    already-established user is never half-locked-out in a confusing way."""
    app = create_app(
        db_path,
        auth_settings=_settings(allowed_emails=frozenset({"colleague@example.com"})),
        identity_provider=provider,
    )
    client = TestClient(app, follow_redirects=False)
    _complete_sign_in(client, provider, identity=COLLEAGUE)

    narrowed = create_app(
        db_path,
        auth_settings=_settings(allowed_emails=frozenset()),
        identity_provider=provider,
    )
    resp = _complete_sign_in(TestClient(narrowed, follow_redirects=False), provider, identity=COLLEAGUE)
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# The session that sign-in issues
# ---------------------------------------------------------------------------


def test_signing_in_issues_a_session_cookie_with_protective_flags(client, provider):
    resp = _complete_sign_in(client, provider)

    header = resp.headers["set-cookie"]
    attributes = {part.strip().lower() for part in header.split(";")}

    assert header.startswith(f"{sessions.COOKIE_NAME}=")
    # HttpOnly: an XSS bug can still act as the user, but cannot walk off
    # with a session that keeps working after the page is closed.
    assert "httponly" in attributes
    # SameSite=Lax: the browser-side half of the CSRF defence.
    assert "samesite=lax" in attributes
    assert "path=/" in attributes


def test_the_session_cookie_expires_rather_than_living_forever(client, provider):
    resp = _complete_sign_in(client, provider)
    max_age = next(
        part.split("=", 1)[1]
        for part in resp.headers["set-cookie"].split("; ")
        if part.lower().startswith("max-age=")
    )
    assert int(max_age) == int(sessions.ABSOLUTE_LIFETIME.total_seconds())


def test_the_cookie_is_marked_secure_for_an_https_deployment(db_path, provider):
    """Derived from the redirect URI rather than configured separately, so
    an HTTPS deployment cannot end up sending the session in the clear."""
    settings = auth.load_auth_settings(
        {
            "RAGRA_OAUTH_CLIENT_ID": "id",
            "RAGRA_OAUTH_CLIENT_SECRET": "secret",
            "RAGRA_OAUTH_REDIRECT_URI": "https://ragra.example/auth/callback",
            "RAGRA_OWNER_EMAIL": "owner@example.com",
        }
    )
    assert settings.secure_cookies is True

    client = TestClient(
        create_app(db_path, auth_settings=settings, identity_provider=provider),
        follow_redirects=False,
    )
    resp = _complete_sign_in(client, provider)
    assert "Secure" in resp.headers["set-cookie"]


def test_a_signed_in_browser_can_read_its_own_dashboard(client, provider):
    _complete_sign_in(client, provider)
    assert client.get("/").status_code == 200


def test_signing_in_again_issues_a_different_session(client, provider):
    """Session fixation defence: the token is always freshly generated, so
    an attacker cannot plant one and wait for it to become authenticated."""
    first = _complete_sign_in(client, provider).headers["set-cookie"]
    second = _complete_sign_in(client, provider).headers["set-cookie"]
    assert first != second


def test_a_planted_cookie_does_not_authenticate(client, provider):
    client.cookies.set(sessions.COOKIE_NAME, "attacker-chosen-value")
    resp = client.get("/", headers={"accept": "text/html"})
    assert resp.status_code == 303


def test_logging_out_revokes_the_session_immediately(client, provider):
    _complete_sign_in(client, provider)
    assert client.get("/").status_code == 200

    resp = client.post("/logout")  # CSRF header set by _complete_sign_in
    assert resp.status_code == 303

    # The cookie is cleared, but the real assertion is that the token no
    # longer works even if a copy of it is presented again.
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_a_revoked_session_token_is_dead_even_if_replayed(client, db_path, provider):
    _complete_sign_in(client, provider)
    token = client.cookies.get(sessions.COOKIE_NAME)
    client.post("/logout")

    client.cookies.set(sessions.COOKIE_NAME, token)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_logout_is_not_reachable_by_a_get(client, provider):
    """A GET sign-out is a URL any page can put in an image tag."""
    _complete_sign_in(client, provider)
    assert client.get("/logout").status_code == 405


# ---------------------------------------------------------------------------
# Cross-account access over HTTP
# ---------------------------------------------------------------------------


def test_one_users_session_cannot_read_another_users_task(db_path, provider):
    """The end-to-end IDOR check: the repository filter is what answers
    this, so it is asserted through the routes rather than only in unit
    tests of the query layer."""
    settings = _settings(allowed_emails=frozenset({"colleague@example.com"}))
    app = create_app(db_path, auth_settings=settings, identity_provider=provider)

    owner_client = TestClient(app, follow_redirects=False)
    _complete_sign_in(owner_client, provider, identity=OWNER)

    with connect_closing(db_path) as conn:
        owner_user = repo.get_user_by_google_sub(conn, google_sub=OWNER.subject)["id"]
        task_id = repo.create_manual_task(conn, user_id=owner_user, title="Private plan")

    colleague_client = TestClient(app, follow_redirects=False)
    _complete_sign_in(colleague_client, provider, identity=COLLEAGUE)

    assert owner_client.get(f"/tasks/{task_id}").status_code == 200

    resp = colleague_client.get(f"/tasks/{task_id}")
    assert resp.status_code == 404
    assert "Private plan" not in resp.text


def test_one_user_cannot_complete_another_users_task(db_path, provider):
    settings = _settings(allowed_emails=frozenset({"colleague@example.com"}))
    app = create_app(db_path, auth_settings=settings, identity_provider=provider)

    owner_client = TestClient(app, follow_redirects=False)
    _complete_sign_in(owner_client, provider, identity=OWNER)
    with connect_closing(db_path) as conn:
        owner_user = repo.get_user_by_google_sub(conn, google_sub=OWNER.subject)["id"]
        task_id = repo.create_manual_task(conn, user_id=owner_user, title="Private plan")

    colleague_client = TestClient(app, follow_redirects=False)
    _complete_sign_in(colleague_client, provider, identity=COLLEAGUE)
    colleague_client.post(f"/tasks/{task_id}/complete")

    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] != "COMPLETED"


def test_a_users_dashboard_never_shows_another_users_work(db_path, provider):
    settings = _settings(allowed_emails=frozenset({"colleague@example.com"}))
    app = create_app(db_path, auth_settings=settings, identity_provider=provider)

    owner_client = TestClient(app, follow_redirects=False)
    _complete_sign_in(owner_client, provider, identity=OWNER)
    with connect_closing(db_path) as conn:
        owner_user = repo.get_user_by_google_sub(conn, google_sub=OWNER.subject)["id"]
        repo.create_manual_task(conn, user_id=owner_user, title="Owner's secret assignment")

    colleague_client = TestClient(app, follow_redirects=False)
    _complete_sign_in(colleague_client, provider, identity=COLLEAGUE)

    for path in ("/", "/tasks", "/missed", "/announcements", "/brief"):
        body = colleague_client.get(path).text
        assert "Owner's secret assignment" not in body


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_sign_in_reports_clearly_when_it_is_not_configured(db_path):
    app = create_app(db_path, auth_settings=_settings(client_id="", client_secret=""))
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/login")
    assert resp.status_code == 503


def test_settings_are_read_from_the_environment(monkeypatch):
    settings = auth.load_auth_settings(
        {
            "RAGRA_OAUTH_CLIENT_ID": "id",
            "RAGRA_OAUTH_CLIENT_SECRET": "secret",
            "RAGRA_OAUTH_REDIRECT_URI": "http://127.0.0.1:8731/auth/callback",
            "RAGRA_OWNER_EMAIL": "Owner@Example.com",
            "RAGRA_ALLOWED_EMAILS": " a@example.com , B@example.com ,, ",
        }
    )
    assert settings.configured
    assert settings.owner_email == "owner@example.com"
    assert settings.allowed_emails == frozenset({"a@example.com", "b@example.com"})
    assert settings.secure_cookies is False


def test_secure_cookies_can_be_forced_on_for_a_proxied_deployment():
    """Behind a TLS-terminating proxy the redirect URI may legitimately be
    http, so this can be turned on - but never off for an https URI."""
    settings = auth.load_auth_settings(
        {
            "RAGRA_OAUTH_REDIRECT_URI": "http://internal:8731/auth/callback",
            "RAGRA_SECURE_COOKIES": "true",
        }
    )
    assert settings.secure_cookies is True


def test_an_unconfigured_localhost_deployment_still_works_for_its_single_user(db_path):
    """The legacy single-user mode. Losing access to your own dashboard the
    moment identity is introduced would be a worse outcome than the risk
    this carries, and all three of its conditions are checked."""
    app = create_app(db_path, auth_settings=_settings(client_id="", client_secret="", redirect_uri=""))
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000))
    assert client.get("/").status_code == 200


def test_the_legacy_fallback_stops_the_moment_a_second_account_exists(db_path):
    app = create_app(db_path, auth_settings=_settings(client_id="", client_secret="", redirect_uri=""))
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000),
                        follow_redirects=False)
    assert client.get("/").status_code == 200

    with connect_closing(db_path) as conn:
        conn.execute(
            """INSERT INTO users (google_sub, email, display_name, created_at, updated_at)
               VALUES ('another', NULL, 'Another', ?, ?)""",
            (repo.now_iso(), repo.now_iso()),
        )
        conn.commit()

    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_the_legacy_fallback_is_not_available_off_this_machine(db_path):
    """The deployment this protects is the dangerous one: bound to a public
    interface with no sign-in configured."""
    app = create_app(db_path, auth_settings=_settings(client_id="", client_secret="", redirect_uri=""))
    client = TestClient(app, client=("203.0.113.7", 5000), follow_redirects=False)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


@pytest.mark.parametrize(
    "header", ["x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host"]
)
def test_the_legacy_fallback_is_not_available_through_a_proxy(db_path, header):
    """A reverse proxy on the same host makes every remote request look
    loopback, which would hand the owner's dashboard to anyone able to
    reach the proxy. The presence of a proxy header is the signal, so a
    forged one only ever costs the forger the fallback."""
    app = create_app(db_path, auth_settings=_settings(client_id="", client_secret="", redirect_uri=""))
    client = TestClient(app, client=("127.0.0.1", 5000), follow_redirects=False)

    response = client.get("/", headers={"accept": "text/html", header: "203.0.113.7"})
    assert response.status_code == 303


def test_the_legacy_fallback_is_off_once_sign_in_is_configured(db_path):
    app = create_app(db_path, auth_settings=_settings())
    client = TestClient(app, client=("127.0.0.1", 5000), follow_redirects=False)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303
