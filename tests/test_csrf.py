"""CSRF protection.

Two kinds of test here, and both are needed.

The behavioural ones send the requests an attacking site would actually
cause a browser to send, and assert they are refused and change nothing.

The structural ones enumerate every route and every template form, so a
handler or a form added next year is covered without anyone remembering to
write a test for it. That is the failure mode this defence actually has:
not a broken check, but a new endpoint that quietly is not behind it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.web import csrf, sessions
from ragra.web.app import create_app
from tests.support import owner_id, sign_in

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "ragra" / "web" / "templates"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "csrf.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def task_id(db_path):
    with connect_closing(db_path) as conn:
        return repo.create_manual_task(conn, user_id=owner_id(conn), title="Finish the lab")


@pytest.fixture
def client(db_path):
    """Signed in, with the CSRF header cleared. Every test below then opts
    in to whatever token it wants to present - which is the point: the
    interesting requests are the ones carrying the wrong token, or none."""
    c = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(c, db_path)
    c.headers.pop(csrf.HEADER_NAME, None)
    return c


def _valid_token(client) -> str:
    return csrf.token_for(client.cookies.get(sessions.COOKIE_NAME))


def _status(db_path, task_id) -> str:
    with connect_closing(db_path) as conn:
        return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]


# ---------------------------------------------------------------------------
# The attack
# ---------------------------------------------------------------------------


def test_a_forged_post_from_another_site_is_refused(client, db_path, task_id):
    """The whole scenario: the browser attaches the session cookie because
    cookies travel by origin, not by who asked. Without the token this
    request is indistinguishable from one the user meant to make."""
    resp = client.post(f"/tasks/{task_id}/complete")

    assert resp.status_code == 403
    assert _status(db_path, task_id) != "COMPLETED"


def test_a_wrong_token_is_refused(client, db_path, task_id):
    resp = client.post(
        f"/tasks/{task_id}/complete", headers={csrf.HEADER_NAME: "a" * 64}
    )
    assert resp.status_code == 403
    assert _status(db_path, task_id) != "COMPLETED"


def test_another_sessions_token_is_refused(client, db_path, task_id):
    """A token is bound to the session presenting it, so one a user obtained
    legitimately elsewhere - a second account, an old session - is not a
    key to this one."""
    with connect_closing(db_path) as conn:
        other = sessions.create_session(
            conn, user_id=owner_id(conn), now=datetime.now(timezone.utc)
        )

    resp = client.post(
        f"/tasks/{task_id}/complete", headers={csrf.HEADER_NAME: csrf.token_for(other)}
    )
    assert resp.status_code == 403
    assert _status(db_path, task_id) != "COMPLETED"


def test_an_empty_token_is_refused(client, db_path, task_id):
    """Guards the obvious bug: if "" compared equal to "" for a session-less
    request, every unauthenticated POST would pass the check."""
    resp = client.post(f"/tasks/{task_id}/complete", headers={csrf.HEADER_NAME: ""})
    assert resp.status_code == 403
    assert _status(db_path, task_id) != "COMPLETED"


def test_the_right_token_is_accepted(client, db_path, task_id):
    """The other half: the defence must not be so blunt that legitimate
    submissions fail, or it would be turned off."""
    resp = client.post(
        f"/tasks/{task_id}/complete", headers={csrf.HEADER_NAME: _valid_token(client)}
    )
    assert resp.status_code == 303
    assert _status(db_path, task_id) == "COMPLETED"


def test_a_token_submitted_as_a_form_field_is_accepted(client, db_path, task_id):
    """Plain HTML forms have no way to set a header, so the field is the
    path that actually matters in this app."""
    resp = client.post(
        f"/tasks/{task_id}/complete", data={csrf.FIELD_NAME: _valid_token(client)}
    )
    assert resp.status_code == 303
    assert _status(db_path, task_id) == "COMPLETED"


def test_the_rest_of_the_form_still_arrives_intact(client, db_path, task_id):
    """The middleware reads the body to find the token, so it has to put it
    back. If it did not, every form would reach its handler empty and the
    breakage would look like a validation bug rather than a plumbing one."""
    resp = client.post(
        f"/tasks/{task_id}/personal-deadline",
        data={
            csrf.FIELD_NAME: _valid_token(client),
            "personal_deadline": "2026-09-20T18:00:00+00:00",
        },
    )
    assert resp.status_code == 303

    with connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT personal_deadline FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["personal_deadline"] == "2026-09-20T18:00:00+00:00"


def test_reading_a_page_needs_no_token(client):
    """GET is exempt because it is not supposed to change anything.
    Requiring a token to read would break every bookmark and link without
    protecting anything."""
    assert client.get("/").status_code == 200


# ---------------------------------------------------------------------------
# The token itself
# ---------------------------------------------------------------------------


def test_the_csrf_token_does_not_reveal_the_session_token(client):
    """One-way by construction, so a token that leaks - a screenshot, a
    stray log line - is not a session."""
    session_token = client.cookies.get(sessions.COOKIE_NAME)
    token = csrf.token_for(session_token)

    assert token != session_token
    assert session_token not in token


def test_every_session_gets_a_different_token(client, db_path):
    with connect_closing(db_path) as conn:
        other = sessions.create_session(
            conn, user_id=owner_id(conn), now=datetime.now(timezone.utc)
        )
    assert csrf.token_for(client.cookies.get(sessions.COOKIE_NAME)) != csrf.token_for(other)


def test_no_session_means_no_token(client):
    assert csrf.token_for(None) == ""
    assert csrf.token_for("") == ""
    assert csrf.verify(submitted="", session_token=None) is False
    assert csrf.verify(submitted=None, session_token="abc") is False


# ---------------------------------------------------------------------------
# Structural: coverage that survives new code
# ---------------------------------------------------------------------------


def _state_changing_routes(app) -> list[tuple[str, str]]:
    routes = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        for method in methods & csrf.UNSAFE_METHODS:
            routes.append((method, route.path))
    return sorted(routes)


def test_there_are_state_changing_routes_to_check(db_path):
    """A wrong path or a changed API would make the sweep below vacuously
    pass, which is the classic way a coverage test stops covering."""
    routes = _state_changing_routes(create_app(db_path))
    assert len(routes) >= 7
    assert ("POST", "/logout") in routes


def test_every_state_changing_route_refuses_a_request_without_a_token(client, db_path):
    """The sweep. A POST handler added tomorrow is covered by this without
    anyone writing a test for it - which is the failure mode this defence
    actually has."""
    unprotected = []
    for method, path in _state_changing_routes(create_app(db_path)):
        # Path parameters are irrelevant: the check runs before routing
        # reaches the handler, so any concrete value exercises it.
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        response = client.request(method, concrete)
        if response.status_code != 403:
            unprotected.append(f"{method} {path} -> {response.status_code}")

    assert not unprotected, f"state-changing routes reachable without a CSRF token: {unprotected}"


def test_every_form_in_every_template_carries_the_token(client):
    """Enforcement is only half of it: a form that does not submit a token
    is a button that silently 403s. This catches the form somebody adds
    without the hidden field."""
    missing = []
    for template in sorted(TEMPLATES_DIR.glob("*.html")):
        html = template.read_text(encoding="utf-8")
        for match in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.DOTALL | re.IGNORECASE):
            opening = match.group(0)[: match.group(0).index(">") + 1]
            if 'method="post"' not in opening.lower():
                continue
            if f'name="{csrf.FIELD_NAME}"' not in match.group(1):
                line = html[: match.start()].count("\n") + 1
                missing.append(f"{template.name}:{line}")

    assert not missing, f"POST forms without a CSRF field: {missing}"


def test_the_template_check_is_looking_at_real_forms():
    """Same guard as above: if the regex stopped matching, the previous test
    would pass while checking nothing."""
    forms = 0
    for template in TEMPLATES_DIR.glob("*.html"):
        html = template.read_text(encoding="utf-8")
        forms += len(re.findall(r'<form\b[^>]*method="post"', html, re.IGNORECASE))
    assert forms >= 10


def test_a_rendered_page_actually_contains_a_usable_token(client, task_id):
    """End to end: the token in the HTML a browser receives is the one the
    middleware will accept. A template global that silently rendered empty
    would pass every check above and break every button."""
    html = client.get("/").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)

    assert match is not None
    assert match.group(1) == _valid_token(client)
