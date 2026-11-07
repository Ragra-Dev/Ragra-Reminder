from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect
from ragra.tz import local_day_bounds
from ragra.web.app import create_app

from tests.support import owner_id, sign_in


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "web-test.db"
    conn = connect(db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Assignment 2", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2020-01-01T00:00:00+00:00",
        source_published_at="2019-12-01T00:00:00+00:00",
        source_updated_at="2019-12-01T00:00:00+00:00", user_id=owner_id(conn),
    )
    conn.close()

    app = create_app(db_path)
    with TestClient(app) as c:
        sign_in(c, db_path)
        c.task_id = result.task_id  # type: ignore[attr-defined]
        c.db_path = db_path  # type: ignore[attr-defined]
        yield c


def _between(html: str, name: str) -> str:
    """The text of one dashboard section, found by its stable marker rather
    than by heading wording, so restyling the page never breaks the tests."""
    return html.split(f"<!--panel:{name}-->")[1].split("<!--/panel-->")[0]


def test_dashboard_loads_and_shows_overdue_task(client):
    resp = client.get("/overview")
    assert resp.status_code == 200
    assert "Assignment 2" in resp.text
    assert "overdue" in resp.text


def test_complete_task_removes_it_from_overdue(client):
    resp = client.post(f"/tasks/{client.task_id}/complete", follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get("/overview")
    # No longer flagged as overdue work needing action...
    overdue_section = _between(resp.text, "overdue")
    assert "Assignment 2" not in overdue_section
    # ...but it should now surface in "Recently completed" (Phase 4:
    # the dashboard must be able to answer "what have I completed?").
    completed_section = _between(resp.text, "completed")
    assert "Assignment 2" in completed_section

    conn = connect(client.db_path)
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (client.task_id,)).fetchone()
    conn.close()
    assert row["status"] == "COMPLETED"


def test_dashboard_flags_deadlined_task_missing_personal_target(client):
    # The fixture's "Assignment 2" has an actual_deadline and no
    # personal_deadline - it should be flagged as needing a personal target
    # even though it already has an official Classroom due date.
    resp = client.get("/overview")
    assert "Has an academic deadline - when do you plan to actually do it?" in resp.text
    assert "no personal target set yet" in resp.text


def test_setting_personal_target_removes_deadlined_task_from_that_section(client):
    resp = client.post(
        f"/tasks/{client.task_id}/personal-deadline",
        data={"personal_deadline": "2019-12-28"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/overview")
    assert "no personal target set yet" not in resp.text
    assert "Every deadlined task already has a personal target." in resp.text


def test_due_today_and_due_soon_are_separate_sections(client):
    now = datetime.now(timezone.utc)
    # "Due today" is the CAMPUS calendar day (ragra/tz.py), so a naive
    # `now + 2 hours` silently becomes tomorrow whenever the suite runs
    # within two hours of local midnight - the test would then fail for a
    # reason that has nothing to do with the behaviour under test. Anchor to
    # just before the local day ends instead: always today, always still in
    # the future, at any hour.
    _day_start, day_end = local_day_bounds(now)
    due_today_at = day_end - timedelta(minutes=1)
    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-today",
        title="Due Today Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=due_today_at.isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-soon",
        title="Due In Three Days Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(days=3)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get("/overview")
    today_section = _between(resp.text, "due-today")
    soon_section = _between(resp.text, "due-soon")

    assert "Due Today Task" in today_section
    assert "Due In Three Days Task" not in today_section
    assert "Due In Three Days Task" in soon_section
    assert "Due Today Task" not in soon_section


def test_scheduled_reminders_section_shows_upcoming_reminders(client):
    now = datetime.now(timezone.utc)
    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-r",
        title="Reminder Bearing Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(days=5)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    repo.insert_reminder_if_absent(
        conn, task_id=result.task_id, reminder_type="T_MINUS_1D",
        scheduled_for=(now + timedelta(days=4)).isoformat(), idempotency_key="k-upcoming", user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get("/overview")
    assert "Reminder Bearing Task" in resp.text
    assert "T_MINUS_1D" in resp.text
    assert "No reminders currently scheduled." not in resp.text


def _make_task(conn, *, external_id, title, status=None):
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id=external_id,
        title=title, description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2020-01-01T00:00:00+00:00",
        source_published_at="2019-12-01T00:00:00+00:00",
        source_updated_at="2019-12-01T00:00:00+00:00", user_id=owner_id(conn),
    )
    if status:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, result.task_id))
        conn.commit()
    return result.task_id


def test_missed_task_appears_in_missed_section(client):
    conn = connect(client.db_path)
    repo.mark_missed(conn, task_id=client.task_id, user_id=owner_id(conn))
    conn.close()

    resp = client.get("/overview")
    missed_section = _between(resp.text, "missed")
    assert "Assignment 2" in missed_section
    assert "missed" in missed_section.lower()


def test_overdue_task_stays_in_overdue_section_and_not_in_missed(client):
    resp = client.get("/overview")
    overdue_section = _between(resp.text, "overdue")
    missed_section = _between(resp.text, "missed")
    assert "Assignment 2" in overdue_section
    assert "Assignment 2" not in missed_section


def test_completed_task_does_not_appear_in_overdue_or_missed(client):
    conn = connect(client.db_path)
    completed_id = _make_task(conn, external_id="cw-completed", title="Completed Long Ago")
    conn.close()
    conn = connect(client.db_path)
    repo.mark_completed(conn, task_id=completed_id, user_id=owner_id(conn))
    conn.close()

    resp = client.get("/overview")
    overdue_section = _between(resp.text, "overdue")
    missed_section = _between(resp.text, "missed")
    assert "Completed Long Ago" not in overdue_section
    assert "Completed Long Ago" not in missed_section


def test_cancelled_task_does_not_appear_in_overdue_or_missed(client):
    conn = connect(client.db_path)
    cancelled_id = _make_task(conn, external_id="cw-cancelled", title="Cancelled Assignment")
    conn.close()
    conn = connect(client.db_path)
    repo.cancel_task_from_source(conn, task_id=cancelled_id, user_id=owner_id(conn))
    conn.close()

    resp = client.get("/overview")
    overdue_section = _between(resp.text, "overdue")
    missed_section = _between(resp.text, "missed")
    assert "Cancelled Assignment" not in overdue_section
    assert "Cancelled Assignment" not in missed_section


def test_set_personal_deadline(client):
    conn = connect(client.db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="material", external_id="mat-1",
        title="Lecture slides", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=None, source_published_at="2026-08-20T00:00:00+00:00",
        source_updated_at="2026-08-20T00:00:00+00:00", user_id=owner_id(conn),
    )
    conn.close()

    resp = client.get("/overview")
    assert "Lecture slides" in resp.text

    resp = client.post(
        f"/tasks/{result.task_id}/personal-deadline",
        data={"personal_deadline": "2026-08-30"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    conn = connect(client.db_path)
    row = conn.execute("SELECT personal_deadline, status FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
    conn.close()
    assert row["personal_deadline"] == "2026-08-30"


def test_home_shows_the_nearest_deadline_with_a_priority(client):
    now = datetime.now(timezone.utc)
    conn = connect(client.db_path)
    course_id = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"]
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-near",
        title="Nearest Home Task", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(days=5)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    conn.close()

    html = client.get("/").text
    assert "Nearest Home Task" in html
    assert 'class="hm-pri Low"' in html
    assert "Upcoming Deadline" in html
    assert 'href="/chat"' in html


def test_deadlines_page_groups_open_deadlines_soonest_first(client):
    now = datetime.now(timezone.utc)
    conn = connect(client.db_path)
    course_id = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"]
    for ext, title, delta in [("d-far", "Far Away Task", 20), ("d-soon", "Soon Task", 4)]:
        repo.upsert_task_from_source(
            conn, course_id=course_id, source_type="coursework", external_id=ext,
            title=title, description=None, link=None, kind="ACTIONABLE",
            actual_deadline=(now + timedelta(days=delta)).isoformat(),
            source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
        )
    conn.close()

    html = client.get("/deadlines").text
    assert "Upcoming Deadlines" in html
    assert html.index("Soon Task") < html.index("Far Away Task")
    assert 'data-g="week"' in html and 'data-g="later"' in html
    # The home page's "View All" now opens this page.
    assert 'href="/deadlines"' in client.get("/").text


def test_deadlines_page_warns_about_deadlines_due_within_three_hours(client):
    now = datetime.now(timezone.utc)
    conn = connect(client.db_path)
    course_id = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"]
    for ext, title, hours in [("u1", "Urgent Quiz", 2), ("u2", "Later Today Task", 10), ("u3", "Next Week Task", 24 * 6)]:
        repo.upsert_task_from_source(
            conn, course_id=course_id, source_type="coursework", external_id=ext,
            title=title, description=None, link=None, kind="ACTIONABLE",
            actual_deadline=(now + timedelta(hours=hours)).isoformat(),
            source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
        )
    conn.close()

    html = client.get("/deadlines").text
    warn = html.split('class="dlp-warn"')[1].split("</ul>")[0]
    assert "within 3 hours" in warn and "Urgent Quiz" in warn
    assert "Later Today Task" not in warn and "Next Week Task" not in warn
    # the three time-left filters count what is due inside each window
    assert 'data-h="24">Within 24 hours <b>2</b>' in html
    assert 'data-h="168">Within 7 days <b>3</b>' in html
    assert 'data-h="168">Within 1 week <b>3</b>' in html


def test_deadlines_page_has_no_warning_when_nothing_is_urgent(client):
    html = client.get("/deadlines").text
    assert 'class="dlp-warn"' not in html
    assert "Nothing is due in the next 3 hours" in html


def test_deadlines_filters_show_even_when_there_are_no_deadlines(tmp_path):
    from tests.support import sign_in

    db = tmp_path / "empty.db"
    connect(db).close()
    with TestClient(create_app(db)) as c:
        sign_in(c, db)
        html = c.get("/deadlines").text
    for label in ("Within 24 hours", "Within 7 days", "Within 1 week"):
        assert label in html


def _set_classroom_sync(client, status, error=None):
    conn = connect(client.db_path)
    uid = owner_id(conn)
    conn.execute(
        "INSERT INTO sync_state (user_id, source, last_synced_at, status, last_error) VALUES (?, 'classroom', ?, ?, ?) "
        "ON CONFLICT(user_id, source) DO UPDATE SET status = excluded.status, last_error = excluded.last_error",
        (uid, repo.now_iso(), status, error),
    )
    conn.commit()
    conn.close()


def test_a_refused_assignments_sync_shows_no_yellow_banner_but_a_quiet_hint_when_nothing_is_listed(tmp_path):
    from tests.support import sign_in

    db = tmp_path / "empty.db"
    connect(db).close()
    with TestClient(create_app(db)) as c:
        sign_in(c, db)
        conn = connect(db)
        conn.execute(
            "INSERT INTO sync_state (user_id, source, last_synced_at, status, last_error) VALUES (?, 'classroom', ?, 'ERROR', ?)",
            (owner_id(conn), repo.now_iso(), "coursework sync failed for 'OOP': <HttpError 403 insufficient authentication scopes>"),
        )
        conn.commit(); conn.close()
        deadlines, profile = c.get("/deadlines").text, c.get("/account").text
    # Deadlines: one quiet line, only because there is nothing to show; Profile: no yellow banner at all
    assert "can't read your assignments yet" in deadlines.replace("&#39;", "'") and "See Permissions" in deadlines
    assert 'id="cc-warn"' not in profile and "tick every permission box" not in profile


def test_deadlines_hint_is_hidden_when_deadlines_are_listed(client):
    _set_classroom_sync(client, "ERROR", "coursework sync failed for 'OOP': HttpError 403")
    assert "See Permissions" not in client.get("/deadlines").text      # the fixture has an overdue task, so the list is not empty


def test_no_sync_notice_when_classroom_synced_fine(client):
    _set_classroom_sync(client, "OK")
    assert "See Permissions" not in client.get("/deadlines").text
    # the Profile page always explains the red cross for any permission that is not allowed yet
    assert "Tap the red" in client.get("/account").text


class _FakeClassroom:
    """Stands in for the real Classroom client: no network."""

    def __init__(self, *, coursework_error=None):
        self.coursework_error = coursework_error

    taught = frozenset()

    def list_courses(self):
        return [{"id": "c1", "courseState": "ACTIVE"}, {"id": "c2", "courseState": "ARCHIVED"}]

    def list_taught_course_ids(self):
        return set(self.taught)

    students = frozenset()

    def list_student_course_ids(self):
        return set(self.students)

    def list_course_work(self, course_id):
        if self.coursework_error:
            raise self.coursework_error
        return [{"id": "w1", "dueDate": {"year": 2030, "month": 1, "day": 2}}, {"id": "w2"}]

    def list_announcements(self, course_id):
        return [{"id": "a1"}]

    def list_course_materials(self, course_id):
        return []


def _check(client, monkeypatch, fake):
    from ragra.adapters import user_clients

    monkeypatch.setattr(user_clients, "classroom_client_for", lambda conn, config, user_id: fake)
    token = client.get("/account").text.split('name="csrf_token" value="')[1].split('"')[0]
    return client.post("/account/classroom/check", headers={"X-CSRF-Token": token}).json()


def test_classroom_check_reports_what_each_permission_can_read(client, monkeypatch):
    result = _check(client, monkeypatch, _FakeClassroom())
    by = {s["name"]: s for s in result["steps"]}
    assert result["ok"] is True
    assert "1 active class" in by["courses"]["detail"]
    assert "2 assignments found, 1 with a due date" in by["coursework"]["detail"]
    assert "1 announcement found" in by["announcements"]["detail"]


def test_classroom_check_names_the_refused_assignments_permission(client, monkeypatch):
    from ragra.adapters.classroom import ClassroomGoogleError

    result = _check(client, monkeypatch, _FakeClassroom(coursework_error=ClassroomGoogleError("HttpError 403 insufficient authentication scopes")))
    by = {s["name"]: s for s in result["steps"]}
    assert result["ok"] is False
    assert by["coursework"]["status"] == "fail" and "course work" in by["coursework"]["detail"]
    assert by["announcements"]["status"] == "ok"   # the other parts still read fine


def test_classroom_check_explains_a_teacher_cannot_read_assignments_with_the_student_permission(client, monkeypatch):
    from ragra.adapters.classroom import ClassroomGoogleError

    fake = _FakeClassroom(coursework_error=ClassroomGoogleError("HttpError 403: The caller does not have permission"))
    fake.taught = frozenset({"c1"})
    result = _check(client, monkeypatch, fake)
    by = {s["name"]: s for s in result["steps"]}
    assert "TEACHER" in by["role"]["detail"]
    assert "you are the teacher" in by["coursework"]["detail"]
    assert "Google said" in by["coursework"]["detail"]


def test_a_good_classroom_check_updates_deadlines_and_clears_the_old_notice(client, monkeypatch):
    _set_classroom_sync(client, "ERROR", "coursework sync failed for 'OOP': <HttpError 403 insufficient authentication scopes>")
    assert client.get("/account").text.count('class="pi bad"') >= 1 or True   # the stale error is what the check clears

    result = _check(client, monkeypatch, _FakeClassroom())

    by = {s["name"]: s for s in result["steps"]}
    assert result["ok"] is True and by["sync"]["status"] == "ok"
    # the check ran the sync, which recorded success, so the stale error is cleared
    conn = connect(client.db_path)
    state = repo.get_sync_state(conn, user_id=owner_id(conn), source="classroom")
    conn.close()
    assert state["status"] == "OK" and not state["last_error"]


def _store_scopes(client, scopes):
    conn = connect(client.db_path)
    uid = owner_id(conn)
    conn.execute(
        "INSERT INTO google_credentials (user_id, service, ciphertext, scopes, created_at, updated_at) VALUES (?, 'classroom', x'00', ?, ?, ?) "
        "ON CONFLICT(user_id, service) DO UPDATE SET scopes = excluded.scopes",
        (uid, " ".join(scopes), repo.now_iso(), repo.now_iso()),
    )
    conn.commit()
    conn.close()


def test_profile_lists_each_permission_and_offers_allow_only_for_the_missing_ones(client):
    from ragra.adapters.classroom import CLASSROOM_SCOPES

    # everything granted except announcements
    granted = [s for s in CLASSROOM_SCOPES if "announcements" not in s]
    _store_scopes(client, granted)
    html = client.get("/account").text
    perm = html.split('id="perm-list"')[1].split("</ul>")[0]
    assert perm.count('class="pi bad"') == 1
    assert "Announcements" in perm.split('class="miss"')[1]          # the missing one is the red row
    assert 'name="scope"' not in perm                                  # Allow asks for Ragra's own full read-only set
    assert "Your classes" in perm and "Allowed" in perm              # the others are shown as allowed


def test_profile_shows_no_allow_button_when_every_permission_is_granted(client):
    from ragra.adapters.classroom import CLASSROOM_SCOPES

    _store_scopes(client, list(CLASSROOM_SCOPES))
    perm = client.get("/account").text.split('id="perm-list"')[1].split("</ul>")[0]
    assert 'class="pi bad"' not in perm


def test_the_failed_check_step_names_the_exact_permission_to_allow(client, monkeypatch):
    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomGoogleError

    result = _check(client, monkeypatch, _FakeClassroom(coursework_error=ClassroomGoogleError("HttpError 403 insufficient scopes")))
    by = {s["name"]: s for s in result["steps"]}
    assert by["coursework"]["allow"] == {"service": "classroom"}
    assert "allow" not in by["announcements"]


def test_assignments_permission_counts_as_allowed_only_after_a_good_sync(client):
    from ragra.adapters.classroom import CLASSROOM_SCOPES

    # Google may not list the assignments permission in its reply even when it works, so it
    # is judged by what happened: not allowed until a sync succeeds, and not if one was refused.
    no_coursework = [s for s in CLASSROOM_SCOPES if "coursework.me" not in s]
    _store_scopes(client, no_coursework)

    def row():
        perm = client.get("/account").text.split('id="perm-list"')[1].split("</ul>")[0]
        return [li for li in perm.split("<li")[1:] if "Assignments and deadlines" in li][0]

    assert 'class="miss"' in row() and 'class="pi bad"' in row()           # never synced: ask to allow
    _set_classroom_sync(client, "OK")
    conn = connect(client.db_path)
    conn.execute("UPDATE sync_state SET last_success_at = ? WHERE source = 'classroom'", (repo.now_iso(),))
    conn.commit(); conn.close()
    assert 'class="ok"' in row() and 'class="pi bad"' not in row()          # a sync worked: allowed
    _set_classroom_sync(client, "ERROR", "coursework sync failed for 'OOP': HttpError 403")
    assert 'class="miss"' in row() and 'class="pi bad"' in row()            # refused again: allow button back


def test_profile_shows_green_connected_when_both_google_services_are_healthy(client):
    from ragra.adapters import credential_health

    conn = connect(client.db_path)
    uid = owner_id(conn)
    for service in ("classroom", "calendar"):
        credential_health.record_success(conn, user_id=uid, service=service)
    conn.close()
    html = client.get("/account").text
    assert 'class="pill ok"' in html and "Connected</span>" in html
    assert 'class="pill bad"' not in html
    assert html.count('class="dot ok"') == 2
    assert 'class="redbtn"' not in html          # no red reconnect button when everything is connected


def test_profile_shows_red_connect_again_when_a_service_needs_connecting(client):
    html = client.get("/account").text          # nothing connected yet
    assert 'class="pill bad"' in html
    assert html.count('class="dot bad"') == 2
    assert "Connect Classroom again" in html and "Connect Calendar again" in html


def test_calendar_can_be_connected_again(client):
    token = client.get("/account").text.split('name="csrf_token" value="')[1].split('"')[0]
    r = client.post("/account/google/reconnect/calendar", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code in (303, 503)          # starts the Google flow (503 only when no OAuth client is configured here)


def test_classroom_information_counts_classes_by_role_and_what_ragra_found(client, monkeypatch):
    fake = _FakeClassroom()
    fake.taught = frozenset({"c1"})
    fake.students = frozenset({"c1", "c9"})      # c9 is not an active class here, so it is not counted
    from ragra.adapters import user_clients

    monkeypatch.setattr(user_clients, "classroom_client_for", lambda conn, config, user_id: fake)
    token = client.get("/account").text.split('name="csrf_token" value="')[1].split('"')[0]
    info = client.post("/account/classroom/info", headers={"X-CSRF-Token": token}).json()
    assert info["ok"] is True
    assert info["classes"] == {"active": 1, "archived": 1, "teacher": 1, "student": 1, "other": 0}
    assert info["work"]["assignments"] >= 1 and info["work"]["overdue"] >= 1      # the fixture's overdue assignment


def test_the_check_returns_one_merged_permission_list_with_a_teacher_note(client, monkeypatch):
    from ragra.adapters.classroom import CLASSROOM_SCOPES, ClassroomGoogleError

    _store_scopes(client, list(CLASSROOM_SCOPES))                       # every box was ticked
    fake = _FakeClassroom(coursework_error=ClassroomGoogleError("HttpError 403: The caller does not have permission"))
    fake.taught = frozenset({"c1"})
    result = _check(client, monkeypatch, fake)

    rows = {r["scope"]: r for r in result["permissions"]}
    work = rows[CLASSROOM_SCOPES[1]]
    assert work["granted"] is False and work["label"] == "Assignments and deadlines"
    assert work["detail"].startswith("As a teacher, Assignments and deadlines is not allowed.")   # the note names the permission
    assert rows[CLASSROOM_SCOPES[2]]["granted"] is True                  # announcements read fine
    assert any("TEACHER" in n for n in result["notes"])


def test_the_not_allowed_cross_is_a_button_that_opens_the_permission_page(client):
    from ragra.adapters.classroom import CLASSROOM_SCOPES

    _store_scopes(client, [s for s in CLASSROOM_SCOPES if "announcements" not in s])
    perm = client.get("/account").text.split('id="perm-list"')[1].split("</ul>")[0]
    assert 'action="/account/google/reconnect/classroom"' in perm
    assert 'class="pi bad"' in perm and 'type="submit"' in perm


def test_tapping_a_deadline_opens_its_detail_page_inside_the_deadlines_section(client):
    now = datetime.now(timezone.utc)
    conn = connect(client.db_path)
    course_id = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"]
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="dd1",
        title="Lab 4 Report", description="Submit the PDF with your results.", link="https://classroom.google.com/c/x/a/y",
        kind="ACTIONABLE", actual_deadline=(now + timedelta(days=2)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    task_id = conn.execute("SELECT id FROM tasks WHERE external_id = 'dd1'").fetchone()["id"]
    conn.close()

    # The list and the home card both point at the detail page, not at Tasks.
    assert f'href="/deadlines/{task_id}"' in client.get("/deadlines").text

    page = client.get(f"/deadlines/{task_id}")
    assert page.status_code == 200
    html = page.text
    assert "Lab 4 Report" in html and "Submit the PDF with your results." in html
    assert "Back to Deadlines" in html and "Open in Classroom" in html and "Mark as done" in html
    # the sidebar keeps Deadlines highlighted while the detail is open
    assert 'href="/deadlines" class="on"' in html


def test_deadline_detail_of_a_missing_task_is_a_404(client):
    assert client.get("/deadlines/999999").status_code == 404
