"""The account settings pages: profile editor, notification preferences,
and the delete-account flow.

The roadmap's Phase 3 frontend work names these explicitly - not just the
storage and CLI commands underneath them - so this is the coverage for the
actual routes and templates, not the repository functions they call (those
are covered in tests/test_user_profiles.py, tests/test_build_providers.py,
and tests/test_account_deletion.py).

Written with the same adversarial posture as the rest of Phase 3: forms are
checked for mass assignment and CSRF, cross-account access is checked
through these specific routes, and the deletion flow is checked for the
one property that matters most - a mistake or a forged request must not be
able to delete an account.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.notifications.preferences import NotificationPreferences, save_preferences
from ragra.relevance.profile import load_raw_profile, save_profile
from ragra.timetable.enrollment import REGULAR, REPEAT, EnrolledCourse
from ragra.web import csrf, sessions
from ragra.web.app import create_app
from tests.support import make_user, owner_id, sign_in


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "account-ui.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def client(db_path):
    c = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(c, db_path)
    return c


@pytest.fixture
def alice(db_path) -> int:
    with connect_closing(db_path) as conn:
        return owner_id(conn)


@pytest.fixture
def bea(db_path) -> int:
    with connect_closing(db_path) as conn:
        return make_user(conn, google_sub="account-ui-second", display_name="Bea")


def _csrf_only_client(db_path):
    """A client with the right session but a cleared CSRF header, for tests
    that need to attach the wrong one deliberately."""
    c = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(c, db_path)
    return c


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


def test_the_account_page_requires_sign_in(db_path):
    client = TestClient(create_app(db_path), follow_redirects=False)
    resp = client.get("/account", headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_the_account_page_shows_the_legacy_owners_existing_enrollment(client):
    """The pre-identity owner's configuration has always lived in
    ragra/timetable/enrollment.py; the editor must open on that, not blank,
    the first time it is visited."""
    body = client.get("/account").text
    assert "Digital Logic" in body
    assert 'value="CS"' in body


def test_a_new_users_account_page_opens_on_an_empty_profile(db_path, bea):
    """The other half of the fallback rule in ragra/relevance/profile.py:
    a second account must not see the first account's courses, including
    on the page whose entire job is showing "your" enrollment.

    Checked against the textarea's actual value, not a bare substring
    search - the field's own placeholder text uses "Digital Logic Design"
    as a formatting example, so a substring check would pass even if the
    fallback leaked real data into the value itself."""
    client = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(client, db_path, user_id=bea)

    body = client.get("/account").text
    match = re.search(r'name="enrollment"[^>]*>(.*?)</textarea>', body, re.DOTALL)
    assert match is not None
    assert match.group(1).strip() == ""
    assert 'value="CS"' not in body  # Alice's program, not Bea's


def test_the_page_shows_this_users_own_saved_notification_destination(client, db_path, alice):
    with connect_closing(db_path) as conn:
        save_preferences(
            conn, user_id=alice,
            preferences=NotificationPreferences(email_enabled=True, email_to="alice@example.com"),
        )

    body = client.get("/account").text
    assert "alice@example.com" in body


def test_the_page_reports_credential_status_without_ever_printing_a_credential(client):
    """Read-only status only - never a token, never a ciphertext."""
    body = client.get("/account").text
    assert "not authorized" in body
    assert "ciphertext" not in body.lower()


# ---------------------------------------------------------------------------
# Profile editing
# ---------------------------------------------------------------------------


def test_saving_a_profile_persists_program_and_enrollment(client, db_path, alice):
    resp = client.post(
        "/account/profile",
        data={
            "program": "EE",
            "batch_year": "2024",
            "enrollment_start_year": "2024",
            "enrollment_start_term": "SPRING",
            "enrollment": "Signals and Systems | EE-A | REGULAR | 2024 | S&S",
        },
    )
    assert resp.status_code == 303

    with connect_closing(db_path) as conn:
        saved = load_raw_profile(conn, user_id=alice)
    assert saved.program == "EE"
    assert saved.enrollment_start_term == "SPRING"
    assert saved.enrollment == (
        EnrolledCourse("Signals and Systems", "EE-A", REGULAR, batch_year="2024", aliases=("S&S",)),
    )


def test_saving_replaces_the_whole_enrollment_not_merges_it(client, db_path, alice):
    """A profile is edited as a whole - the same contract save_profile
    itself enforces, checked here through the route that actually calls
    it."""
    client.post(
        "/account/profile",
        data={
            "program": "CS", "batch_year": "", "enrollment_start_year": "2025",
            "enrollment_start_term": "FALL", "enrollment": "First | A | REGULAR",
        },
    )
    client.post(
        "/account/profile",
        data={
            "program": "CS", "batch_year": "", "enrollment_start_year": "2025",
            "enrollment_start_term": "FALL", "enrollment": "Second | B | REGULAR",
        },
    )

    with connect_closing(db_path) as conn:
        saved = load_raw_profile(conn, user_id=alice)
    assert [c.course_name for c in saved.enrollment] == ["Second"]


def test_an_empty_enrollment_is_accepted(client, db_path, alice):
    """A user with no courses yet must still be able to save a profile."""
    resp = client.post(
        "/account/profile",
        data={
            "program": "CS", "batch_year": "", "enrollment_start_year": "2025",
            "enrollment_start_term": "FALL", "enrollment": "",
        },
    )
    assert resp.status_code == 303
    with connect_closing(db_path) as conn:
        assert load_raw_profile(conn, user_id=alice).enrollment == ()


@pytest.mark.parametrize(
    ("enrollment_start_year", "enrollment", "reason"),
    [
        ("not-a-number", "", "non-numeric year"),
        ("2025", "Only Two Fields | A", "too few fields"),
        ("2025", "Course | A | SOMETIMES", "invalid enrollment type"),
        ("2025", " | A | REGULAR", "blank course name"),
    ],
)
def test_a_malformed_submission_is_rejected_and_nothing_is_saved(
    client, db_path, alice, enrollment_start_year, enrollment, reason
):
    with connect_closing(db_path) as conn:
        before = load_raw_profile(conn, user_id=alice)

    resp = client.post(
        "/account/profile",
        data={
            "program": "CS", "batch_year": "", "enrollment_start_year": enrollment_start_year,
            "enrollment_start_term": "FALL", "enrollment": enrollment,
        },
    )
    assert resp.status_code == 400, reason

    with connect_closing(db_path) as conn:
        after = load_raw_profile(conn, user_id=alice)
    assert after == before, f"a rejected submission changed the stored profile ({reason})"


def test_a_saved_profile_round_trips_through_the_editors_own_text_format(client, db_path, alice):
    """The textarea format is parsed on the way in and rendered on the way
    out; this proves the two directions actually agree, rather than just
    each having its own passing test."""
    client.post(
        "/account/profile",
        data={
            "program": "CS", "batch_year": "2025", "enrollment_start_year": "2025",
            "enrollment_start_term": "FALL",
            "enrollment": "DLD | CS-G | REGULAR | 2025 | Digital Logic\nOOP | CS-C | REPEAT",
        },
    )

    body = client.get("/account").text
    match = re.search(r'name="enrollment"[^>]*>(.*?)</textarea>', body, re.DOTALL)
    assert match is not None
    lines = {line.strip() for line in match.group(1).strip().splitlines()}
    assert "DLD | CS-G | REGULAR | 2025 | Digital Logic" in lines
    assert "OOP | CS-C | REPEAT" in lines


def test_a_forged_user_id_field_cannot_redirect_a_profile_save(client, db_path, alice, bea):
    """Mass assignment: the route does not declare user_id, so no amount
    of extra POST data can set whose profile is written."""
    client.post(
        "/account/profile",
        data={
            "program": "CS", "batch_year": "", "enrollment_start_year": "2025",
            "enrollment_start_term": "FALL", "enrollment": "",
            "user_id": str(bea),
        },
    )

    with connect_closing(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM user_profiles WHERE user_id = ?", (bea,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM user_profiles WHERE user_id = ?", (alice,)
        ).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------


def test_saving_notification_preferences_persists_them(client, db_path, alice):
    resp = client.post(
        "/account/notifications",
        data={"email_enabled": "1", "email_to": "me@example.com", "hermes_target": "whatsapp:1"},
    )
    assert resp.status_code == 303

    with connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM notification_preferences WHERE user_id = ?", (alice,)
        ).fetchone()
    assert row["email_enabled"] == 1
    assert row["email_to"] == "me@example.com"
    # hermes_target was supplied but hermes_enabled was not checked - the
    # address is kept, delivery is not turned on.
    assert row["hermes_enabled"] == 0
    assert row["hermes_target"] == "whatsapp:1"


def test_unchecking_a_channel_keeps_its_address(client, db_path, alice):
    """Enabled and destination are saved independently so switching a
    channel off never discards the address it will be switched back on
    with."""
    client.post(
        "/account/notifications", data={"email_enabled": "1", "email_to": "me@example.com"}
    )
    client.post("/account/notifications", data={"email_to": "me@example.com"})  # unchecked

    with connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT email_enabled, email_to FROM notification_preferences WHERE user_id = ?",
            (alice,),
        ).fetchone()
    assert row["email_enabled"] == 0
    assert row["email_to"] == "me@example.com"


def test_a_forged_user_id_field_cannot_redirect_a_preference_save(client, db_path, alice, bea):
    client.post(
        "/account/notifications",
        data={"email_enabled": "1", "email_to": "me@example.com", "user_id": str(bea)},
    )

    with connect_closing(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM notification_preferences WHERE user_id = ?", (bea,)
        ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------


def test_the_delete_confirmation_page_names_what_will_be_removed(client, db_path, alice):
    with connect_closing(db_path) as conn:
        repo.create_manual_task(conn, user_id=alice, title="A private plan")

    body = client.get("/account/delete").text
    assert "tasks:" in body
    assert "cannot be undone" in body.lower()


def test_visiting_the_confirmation_page_deletes_nothing(client, db_path, alice):
    client.get("/account/delete")

    with connect_closing(db_path) as conn:
        assert repo.get_user(conn, user_id=alice) is not None


@pytest.mark.parametrize("confirm", ["", "yes", "DELETE ME", "delet", "please delete"])
def test_anything_other_than_the_confirmation_word_is_refused(
    client, db_path, alice, confirm
):
    """Exact match required - a checkbox ticked in passing, or a
    plausible-looking phrase, must not be enough for the one action in
    Ragra that cannot be undone. Matching is case-insensitive (see the
    acceptance test below): that is a deliberate usability choice, not a
    security boundary - the real boundary is the session and CSRF token
    already required to reach this route at all."""
    resp = client.post("/account/delete", data={"confirm": confirm})
    assert resp.status_code == 400

    with connect_closing(db_path) as conn:
        assert repo.get_user(conn, user_id=alice) is not None


@pytest.mark.parametrize("confirm", ["delete", "Delete", "DELETE", "  delete  "])
def test_the_confirmation_word_is_accepted_case_insensitively_and_untrimmed(
    db_path, confirm
):
    """States the accepted forms explicitly, so the leniency is a tested
    decision rather than an accident of whichever case someone tried."""
    with connect_closing(db_path) as conn:
        user_id = owner_id(conn)
    client = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(client, db_path, user_id=user_id)

    resp = client.post("/account/delete", data={"confirm": confirm})

    assert resp.status_code == 303
    with connect_closing(db_path) as conn:
        assert repo.get_user(conn, user_id=user_id) is None


def test_a_successful_deletion_redirects_to_sign_in(client, db_path, alice):
    resp = client.post("/account/delete", data={"confirm": "delete"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    with connect_closing(db_path) as conn:
        assert repo.get_user(conn, user_id=alice) is None


def test_the_session_used_to_delete_the_account_stops_working_immediately(client, db_path, alice):
    token = client.cookies.get(sessions.COOKIE_NAME)
    client.post("/account/delete", data={"confirm": "delete"})

    with connect_closing(db_path) as conn:
        assert sessions.lookup_session(conn, token=token, now=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )) is None


def test_a_forged_user_id_cannot_delete_another_account(client, db_path, alice, bea):
    """The sharpest possible mass-assignment case: the route resolves the
    account to delete from the session alone, so no form field can name a
    different one."""
    client.post("/account/delete", data={"confirm": "delete", "user_id": str(bea)})

    with connect_closing(db_path) as conn:
        assert repo.get_user(conn, user_id=alice) is None  # the caller's own account
        assert repo.get_user(conn, user_id=bea) is not None  # untouched


def test_deleting_removes_only_the_acting_accounts_data(client, db_path, alice, bea):
    with connect_closing(db_path) as conn:
        repo.create_manual_task(conn, user_id=alice, title="Alice's task")
        bea_task = repo.create_manual_task(conn, user_id=bea, title="Bea's task")

    client.post("/account/delete", data={"confirm": "delete"})

    with connect_closing(db_path) as conn:
        assert repo.get_task_by_id(conn, user_id=bea, task_id=bea_task) is not None


# ---------------------------------------------------------------------------
# CSRF, applied to these routes via the same middleware as everything else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/account/profile", "/account/notifications", "/account/delete"]
)
def test_every_account_route_refuses_a_request_without_a_csrf_token(db_path, path):
    client = _csrf_only_client(db_path)
    client.headers.pop(csrf.HEADER_NAME, None)

    resp = client.post(path, data={"confirm": "delete", "program": "CS",
                                    "enrollment_start_year": "2025",
                                    "enrollment_start_term": "FALL"})
    assert resp.status_code == 403


def test_every_form_on_the_account_page_carries_the_csrf_field(client):
    body = client.get("/account").text
    forms = re.findall(r"<form\b[^>]*method=\"post\"[^>]*>(.*?)</form>", body, re.DOTALL)
    assert len(forms) >= 2
    for form in forms:
        assert 'name="csrf_token"' in form


def test_the_delete_confirmation_form_carries_the_csrf_field(client):
    body = client.get("/account/delete").text
    assert 'name="csrf_token"' in body


# ---------------------------------------------------------------------------
# Cross-account reading
# ---------------------------------------------------------------------------


def test_no_account_page_shows_another_accounts_profile_or_preferences(db_path, alice, bea):
    with connect_closing(db_path) as conn:
        save_profile(
            conn, user_id=alice, program="CS", batch_year="2025",
            enrollment_start_year=2025, enrollment_start_term="FALL",
            enrollment=(EnrolledCourse("AliceOnlyCourse", "CS-G", REGULAR),),
        )
        save_preferences(
            conn, user_id=alice,
            preferences=NotificationPreferences(email_enabled=True, email_to="alice@example.com"),
        )

    bea_client = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(bea_client, db_path, user_id=bea)

    body = bea_client.get("/account").text
    assert "AliceOnlyCourse" not in body
    assert "alice@example.com" not in body
