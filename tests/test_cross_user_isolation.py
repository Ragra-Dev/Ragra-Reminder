"""Cross-user isolation, exercised through the HTTP surface.

tests/test_user_isolation.py proves the repository layer filters by owner.
This file proves the thing a user actually cares about: that with two real
accounts signed in to the running app, neither can see or change anything
belonging to the other, by any route.

Written adversarially throughout. Two accounts are set up with deliberately
colliding data - the same Classroom course, the same timetable slot, tasks
created back to back so their ids are adjacent and guessable - and then one
account attempts everything the other can do. A suite built from two users
with unrelated data would pass while still leaking.

The route sweep at the end is the part that survives new code: it walks the
app's own routing table, so an endpoint added next year is attacked by
these tests without anyone remembering to add it.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.web import csrf, sessions
from ragra.web.app import create_app
from tests.support import make_user, owner_id, sign_in

# Strings that must never appear in the other account's responses. Distinct
# enough that a substring match is meaningful.
ALICE_SECRET = "AliceOnlyAssignmentTitle"
ALICE_ANNOUNCEMENT = "AliceOnlyAnnouncement"
BEA_SECRET = "BeaOnlyAssignmentTitle"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "isolation.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def accounts(db_path):
    """Two accounts whose data collides in every way that matters."""
    with connect_closing(db_path) as conn:
        alice = owner_id(conn)
        bea = make_user(conn, google_sub="cross-user-second", display_name="Bea")

        data = {"alice": {}, "bea": {}}
        for name, user_id, secret in (
            ("Alice", alice, ALICE_SECRET),
            ("Bea", bea, BEA_SECRET),
        ):
            # The same Classroom course id for both: classmates share one.
            course_id = repo.upsert_course(
                conn, user_id=user_id, external_id="shared-course", name="Digital Logic",
                section="A", teacher=None, course_code="EE1005", state="ACTIVE",
            )
            coursework = repo.upsert_task_from_source(
                conn, user_id=user_id, course_id=course_id, source_type="coursework",
                external_id="shared-item", title=secret, description=f"{secret} body",
                link="http://classroom.example/x", kind="ACTIONABLE",
                actual_deadline="2020-01-01T00:00:00+00:00",
                source_published_at=None, source_updated_at=None,
            ).task_id
            manual = repo.create_manual_task(conn, user_id=user_id, title=f"{secret} manual")
            announcement = repo.upsert_task_from_source(
                conn, user_id=user_id, course_id=course_id, source_type="announcement",
                external_id="shared-announcement",
                title=ALICE_ANNOUNCEMENT if name == "Alice" else "BeaOnlyAnnouncement",
                description=None, link=None, kind="INFORMATIONAL", actual_deadline=None,
                source_published_at="2026-09-01T10:00:00+00:00", source_updated_at=None,
            ).task_id
            repo.mark_overdue_tasks_as_missed(
                conn, user_id=user_id, now="2026-09-01T00:00:00+00:00"
            )
            repo.record_notification_delivery(
                conn, user_id=user_id, provider=f"{name}Provider", ok=True,
                error=f"{secret} delivery note",
            )
            data[name.lower()] = {
                "user_id": user_id,
                "coursework": coursework,
                "manual": manual,
                "announcement": announcement,
                "course": course_id,
            }
    return data


def _client(db_path, user_id):
    client = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(client, db_path, user_id=user_id)
    return client


@pytest.fixture
def alice_client(db_path, accounts):
    return _client(db_path, accounts["alice"]["user_id"])


@pytest.fixture
def bea_client(db_path, accounts):
    return _client(db_path, accounts["bea"]["user_id"])


def _snapshot(db_path) -> list[tuple]:
    """Every row of every user-owned table, for before/after comparison.

    Blunt on purpose: asserting "nothing anywhere changed" catches a write
    landing in a table the test author did not think to check, which is
    exactly the kind of write that gets missed."""
    from ragra import accounts as accounts_module

    rows = []
    with connect_closing(db_path) as conn:
        for table in accounts_module.owned_tables(conn):
            if table == "sessions":
                continue  # last_seen_at legitimately moves on every request
            for row in conn.execute(f"SELECT * FROM {table}"):
                rows.append((table, tuple(row)))
    return rows


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/missed", "/tasks", "/announcements", "/deliveries", "/brief"])
def test_no_page_shows_the_other_accounts_content(bea_client, path):
    body = bea_client.get(path).text

    assert ALICE_SECRET not in body
    assert ALICE_ANNOUNCEMENT not in body


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", ALICE_SECRET),
        ("/missed", ALICE_SECRET),
        ("/tasks", ALICE_SECRET),
        ("/announcements", ALICE_ANNOUNCEMENT),
        ("/deliveries", "AliceProvider"),
    ],
)
def test_each_account_does_see_its_own_content(alice_client, path, expected):
    """The other half, and not a formality: an app that showed nobody
    anything would pass every isolation test above. Each page is checked
    for something only this account has, so "isolated" cannot be achieved
    by rendering nothing."""
    assert expected in alice_client.get(path).text


def test_the_brief_reports_this_accounts_own_state(alice_client, bea_client):
    """The brief is generated text rather than a listing, so it is checked
    on its own terms: Alice's overdue task is MISSED and therefore counted,
    not listed. What matters is that Bea's brief never mentions it."""
    alice_brief = alice_client.get("/brief").text
    bea_brief = bea_client.get("/brief").text

    assert "OVERDUE" in alice_brief
    assert ALICE_SECRET not in bea_brief
    assert BEA_SECRET not in alice_brief


def test_the_dashboard_shows_each_account_its_own_work(alice_client, bea_client):
    assert ALICE_SECRET in alice_client.get("/tasks").text
    assert BEA_SECRET in bea_client.get("/tasks").text


@pytest.mark.parametrize("resource", ["coursework", "manual", "announcement"])
def test_task_detail_is_a_404_for_another_accounts_task(
    accounts, alice_client, bea_client, resource
):
    """A 404 rather than a 403: confirming that an id exists is itself a
    disclosure, and the ids here are adjacent integers."""
    task_id = accounts["alice"][resource]

    assert alice_client.get(f"/tasks/{task_id}").status_code == 200

    response = bea_client.get(f"/tasks/{task_id}")
    assert response.status_code == 404
    assert ALICE_SECRET not in response.text


def test_a_404_for_another_users_task_is_indistinguishable_from_a_missing_one(
    accounts, bea_client
):
    real = bea_client.get(f"/tasks/{accounts['alice']['manual']}")
    imaginary = bea_client.get("/tasks/99999")

    assert real.status_code == imaginary.status_code == 404
    assert real.text == imaginary.text


def test_delivery_history_does_not_leak_across_accounts(bea_client):
    """The /deliveries page renders stored provider errors, which can carry
    task titles."""
    body = bea_client.get("/deliveries").text

    assert "AliceProvider" not in body
    assert ALICE_SECRET not in body


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_completing_another_accounts_task_changes_nothing(db_path, accounts, bea_client):
    task_id = accounts["alice"]["manual"]
    before = _snapshot(db_path)

    bea_client.post(f"/tasks/{task_id}/complete")

    assert _snapshot(db_path) == before


def test_cancelling_another_accounts_task_changes_nothing(db_path, accounts, bea_client):
    task_id = accounts["alice"]["manual"]
    before = _snapshot(db_path)

    bea_client.post(f"/tasks/{task_id}/cancel")

    assert _snapshot(db_path) == before


def test_editing_another_accounts_task_changes_nothing(db_path, accounts, bea_client):
    task_id = accounts["alice"]["manual"]
    before = _snapshot(db_path)

    bea_client.post(
        f"/tasks/{task_id}/edit",
        data={"title": "hijacked", "description": "hijacked", "actual_deadline": ""},
    )

    assert _snapshot(db_path) == before


def test_setting_a_personal_deadline_on_another_accounts_task_changes_nothing(
    db_path, accounts, bea_client
):
    task_id = accounts["alice"]["coursework"]
    before = _snapshot(db_path)

    bea_client.post(
        f"/tasks/{task_id}/personal-deadline",
        data={"personal_deadline": "2026-09-20T18:00:00+00:00"},
    )

    assert _snapshot(db_path) == before


def test_archiving_another_accounts_announcement_changes_nothing(db_path, accounts, bea_client):
    announcement_id = accounts["alice"]["announcement"]
    before = _snapshot(db_path)

    bea_client.post(f"/announcements/{announcement_id}/archive")

    assert _snapshot(db_path) == before


def test_creating_a_task_from_another_accounts_announcement_creates_nothing(
    db_path, accounts, bea_client
):
    """The subtlest of these: the route creates a *new* task, so a naive
    implementation would happily create it for the wrong owner - and the
    new task would carry the other account's announcement title."""
    announcement_id = accounts["alice"]["announcement"]
    before = _snapshot(db_path)

    bea_client.post(f"/announcements/{announcement_id}/create-task", data={})

    assert _snapshot(db_path) == before
    assert ALICE_ANNOUNCEMENT not in bea_client.get("/tasks").text


def test_a_new_task_belongs_to_the_account_that_created_it(db_path, accounts, bea_client):
    bea_client.post("/tasks/new", data={"title": "Bea's brand new task"})

    with connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM tasks WHERE title = 'Bea''s brand new task'"
        ).fetchone()
    assert row["user_id"] == accounts["bea"]["user_id"]


def test_a_forged_owner_field_cannot_reassign_a_new_task(db_path, accounts, bea_client):
    """Mass assignment: the route does not declare user_id, so no amount of
    extra POST data can set it. Asserted rather than assumed, because the
    consequence would be one account writing rows into another."""
    bea_client.post(
        "/tasks/new",
        data={
            "title": "Planted task",
            "user_id": str(accounts["alice"]["user_id"]),
            "owner_id": str(accounts["alice"]["user_id"]),
            "course_id": str(accounts["alice"]["course"]),
        },
    )

    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT user_id FROM tasks WHERE title = 'Planted task'").fetchone()
    assert row["user_id"] == accounts["bea"]["user_id"]


def test_a_forged_owner_field_cannot_move_an_existing_task(db_path, accounts, bea_client):
    """The other direction: editing your own task must not let you hand it
    to - or take it from - somebody else."""
    own_task = accounts["bea"]["manual"]

    bea_client.post(
        f"/tasks/{own_task}/edit",
        data={
            "title": "Renamed",
            "user_id": str(accounts["alice"]["user_id"]),
            "id": str(accounts["alice"]["manual"]),
        },
    )

    with connect_closing(db_path) as conn:
        moved = conn.execute("SELECT user_id FROM tasks WHERE id = ?", (own_task,)).fetchone()
        victim = conn.execute(
            "SELECT title FROM tasks WHERE id = ?", (accounts["alice"]["manual"],)
        ).fetchone()

    assert moved["user_id"] == accounts["bea"]["user_id"]
    assert victim["title"].startswith(ALICE_SECRET)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_a_session_cannot_be_repointed_at_another_account(db_path, accounts, bea_client):
    """The session row names its owner; a cookie is only ever a lookup key.
    Presenting a token that is not yours simply does not resolve."""
    with connect_closing(db_path) as conn:
        alice_token = sessions.create_session(
            conn, user_id=accounts["alice"]["user_id"],
            now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )

    # Bea presents Alice's CSRF token with her own session: the mismatch is
    # what stops a token obtained elsewhere being usable here.
    response = bea_client.post(
        f"/tasks/{accounts['alice']['manual']}/complete",
        headers={csrf.HEADER_NAME: csrf.token_for(alice_token)},
    )
    assert response.status_code == 403


def test_signing_out_of_one_account_does_not_sign_out_the_other(
    accounts, alice_client, bea_client
):
    bea_client.post("/logout")

    assert alice_client.get("/").status_code == 200
    assert bea_client.get("/", headers={"accept": "text/html"}).status_code == 303


# ---------------------------------------------------------------------------
# The sweep: coverage that survives new routes
# ---------------------------------------------------------------------------


def _routes(app):
    """Every routed endpoint that takes a resource id.

    Parameterised routes only: the sweep works by pointing a route at
    *another account's* id, which is meaningless for an endpoint that takes
    none. Collection endpoints are covered by the page tests above, and
    including a creation route like POST /tasks/new here would have the
    sweep legitimately create a task and then flag its own write.
    """
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "{" not in path:
            continue
        for method in methods & {"GET", "POST"}:
            yield method, path


def test_the_sweep_has_routes_to_sweep(db_path):
    """A changed path shape would make the sweep vacuously pass."""
    routes = list(_routes(create_app(db_path)))
    assert len(routes) >= 7
    assert ("GET", "/tasks/{task_id}") in routes
    assert ("POST", "/tasks/{task_id}/complete") in routes
    assert ("POST", "/announcements/{task_id}/archive") in routes


def test_no_route_leaks_or_mutates_across_accounts(db_path, accounts, bea_client):
    """Walks the app's own routing table and points every route at the
    other account's resources. An endpoint added next year is attacked by
    this test without anyone remembering to add it.

    Two assertions per route, because the two failures are different: a
    response that carries the other account's data, and a request that
    changes it.
    """
    victim_ids = {
        accounts["alice"]["coursework"],
        accounts["alice"]["manual"],
        accounts["alice"]["announcement"],
    }
    before = _snapshot(db_path)
    leaked: list[str] = []

    for method, path in sorted(_routes(create_app(db_path))):
        for victim in sorted(victim_ids):
            concrete = re.sub(r"\{[^}]+\}", str(victim), path)
            response = bea_client.request(
                method,
                concrete,
                data={"title": "sweep", "personal_deadline": "2026-09-20T18:00:00+00:00"}
                if method == "POST"
                else None,
            )
            body = response.text
            if ALICE_SECRET in body or ALICE_ANNOUNCEMENT in body:
                leaked.append(f"{method} {concrete} -> {response.status_code}")

    assert not leaked, f"routes leaking another account's data: {leaked}"
    assert _snapshot(db_path) == before, "a cross-account request changed stored data"
