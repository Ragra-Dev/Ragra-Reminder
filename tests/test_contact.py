from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect
from ragra.web import auth
from ragra.web.app import create_app

from tests.support import make_user, owner_id, sign_in


def _app_client(tmp_path: Path, *, owner_email: str | None = None):
    db_path = tmp_path / "contact.db"
    conn = connect(db_path)
    if owner_email:
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (owner_email, owner_id(conn)))
        conn.commit()
    conn.close()
    settings = auth.AuthSettings(
        client_id="x", client_secret="y", redirect_uri="http://localhost/auth/callback",
        owner_email=owner_email, allowed_emails=frozenset(), secure_cookies=False,
    )
    app = create_app(db_path, auth_settings=settings)
    c = TestClient(app)
    c.__enter__()
    sign_in(c, db_path)
    c.db_path = db_path  # type: ignore[attr-defined]
    return c


@pytest.fixture
def client(tmp_path: Path):
    c = _app_client(tmp_path)
    yield c
    c.__exit__(None, None, None)


def test_contact_page_shows_both_people_and_the_feedback_box(client):
    html = client.get("/contact").text
    assert "Aoun Abbas" in html and "Hashim Amjid" in html
    assert 'href="mailto:aounabbassadhana@gmail.com"' in html      # Aoun's email is a tap-to-write link
    assert 'href="mailto:hashimamjad047@gmail.com"' in html
    assert 'src="/static/icons/hashim-amjid.jpg"' in html          # Hashim's photo
    assert 'src="/static/icons/aoun-abbas.jpg"' in html            # Aoun's photo
    assert client.get("/static/icons/hashim-amjid.jpg").status_code == 200
    assert client.get("/static/icons/aoun-abbas.jpg").status_code == 200
    assert 'action="/contact/feedback"' in html and "<textarea" in html
    assert 'href="/contact" class="on"' in html                    # the sidebar highlights Contact us


def test_sending_feedback_saves_it_and_confirms(client):
    resp = client.post("/contact/feedback", data={"kind": "bug", "rating": "4", "message": "The deadline page is slow."}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/contact?sent=1"
    assert "saved and sent to the team" in client.get("/contact?sent=1").text

    conn = connect(client.db_path)
    row = conn.execute("SELECT * FROM feedback").fetchone()
    conn.close()
    assert row["kind"] == "bug" and row["rating"] == 4 and row["message"] == "The deadline page is slow."
    assert row["user_id"] == owner_id(connect(client.db_path))


def test_an_empty_or_tiny_message_is_not_saved(client):
    resp = client.post("/contact/feedback", data={"message": "  hi "}, follow_redirects=False)
    assert "error=" in resp.headers["location"]
    conn = connect(client.db_path)
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    conn.close()


def test_unknown_kind_and_rating_are_tidied_up(client):
    client.post("/contact/feedback", data={"kind": "<script>", "rating": "99", "message": "A perfectly fine message."})
    conn = connect(client.db_path)
    row = conn.execute("SELECT kind, rating FROM feedback").fetchone()
    conn.close()
    assert row["kind"] == "suggestion" and row["rating"] is None


def test_feedback_is_limited_to_a_few_per_hour(client):
    for i in range(repo.FEEDBACK_MAX_PER_HOUR + 2):
        client.post("/contact/feedback", data={"message": f"Suggestion number {i} for you."})
    conn = connect(client.db_path)
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == repo.FEEDBACK_MAX_PER_HOUR
    conn.close()


def test_the_inbox_is_hidden_from_everyone_but_the_owner(client):
    assert client.get("/contact/inbox").status_code == 404
    assert "read all feedback" not in client.get("/contact").text


def test_the_owner_can_read_everyones_feedback(tmp_path):
    c = _app_client(tmp_path, owner_email="owner@example.com")
    try:
        conn = connect(c.db_path)
        other = make_user(conn, google_sub="sub-other", display_name="Sana")
        repo.add_feedback(conn, user_id=other, kind="praise", rating=5, message="Love the deadline reminders!")
        conn.close()
        assert "read all feedback" in c.get("/contact").text
        page = c.get("/contact/inbox")
        assert page.status_code == 200
        assert "Love the deadline reminders!" in page.text and "Sana" in page.text
    finally:
        c.__exit__(None, None, None)


def test_feedback_is_deleted_with_the_account(client):
    client.post("/contact/feedback", data={"message": "Please add dark mode."})
    conn = connect(client.db_path)
    repo.delete_user(conn, user_id=owner_id(conn))
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Feedback is emailed to the team
# ---------------------------------------------------------------------------

from ragra import feedback_mail  # noqa: E402
from ragra.adapters.notify import NotifyResult  # noqa: E402


class _FakeMailer:
    def __init__(self, to, ok=True):
        self.to, self.ok, self.sent = to, ok, []

    def send(self, notification):
        self.sent.append((self.to, notification))
        return NotifyResult(ok=True) if self.ok else NotifyResult(ok=False, error="EMAIL_TRANSPORT_ERROR: HTTP 401")


def _row(client):
    conn = connect(client.db_path)
    row = conn.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row


def test_feedback_is_emailed_to_both_team_members(client, monkeypatch):
    mailers = []

    def fake_providers(config, recipients):
        mailers.extend(_FakeMailer(to) for to in recipients)
        return mailers

    monkeypatch.setattr(feedback_mail, "providers_for", fake_providers)
    client.post("/contact/feedback", data={"kind": "bug", "rating": "4", "message": "The deadline page is slow."})

    assert {m.to for m in mailers} == {"aounabbassadhana@gmail.com", "hashimamjad047@gmail.com"}
    notification = mailers[0].sent[0][1]
    assert "The deadline page is slow." in notification.text and "Problem" in notification.text
    assert "★★★★☆" in notification.text and "Feedback (problem)" in notification.category
    row = _row(client)
    assert row["emailed_at"] and row["email_note"] == "Sent to 2 of 2 team members."


def test_feedback_is_still_saved_when_email_is_not_set_up(client, monkeypatch):
    monkeypatch.setattr(feedback_mail, "providers_for", lambda config, recipients: [])
    resp = client.post("/contact/feedback", data={"message": "Please add dark mode."}, follow_redirects=False)
    assert resp.headers["location"] == "/contact?sent=1"
    row = _row(client)
    assert row["message"] == "Please add dark mode." and row["emailed_at"] is None
    assert row["email_note"] == feedback_mail.NOT_SET_UP


def test_a_failed_email_is_recorded_and_never_loses_the_feedback(client, monkeypatch):
    monkeypatch.setattr(feedback_mail, "providers_for", lambda config, recipients: [_FakeMailer(t, ok=False) for t in recipients])
    client.post("/contact/feedback", data={"message": "Something is odd on Missed."})
    row = _row(client)
    assert row["message"] == "Something is odd on Missed." and row["emailed_at"] is None
    assert "Sent to 0 of 2" in row["email_note"] and "HTTP 401" in row["email_note"]


def test_a_crash_while_emailing_is_recorded_not_raised(client, monkeypatch):
    def boom(config, recipients):
        raise RuntimeError("mail server exploded")

    monkeypatch.setattr(feedback_mail, "providers_for", boom)
    assert client.post("/contact/feedback", data={"message": "Anything at all here."}).status_code == 200
    assert "mail server exploded" in _row(client)["email_note"]


def test_recipients_default_to_the_team_and_can_be_overridden(monkeypatch):
    monkeypatch.delenv("RAGRA_FEEDBACK_TO", raising=False)
    assert feedback_mail.team_recipients() == ["aounabbassadhana@gmail.com", "hashimamjad047@gmail.com"]
    monkeypatch.setenv("RAGRA_FEEDBACK_TO", "a@x.com, b@y.com ,nonsense")
    assert feedback_mail.team_recipients() == ["a@x.com", "b@y.com"]


def test_the_email_subject_cannot_be_hijacked_by_a_nasty_name():
    n = feedback_mail.build_notification(kind="bug", rating=None, message="hi there", name="Eve\r\nBcc: x@y.com", email=None, when="now")
    assert "\n" not in n.category and "\r" not in n.category


def test_the_owner_inbox_shows_whether_it_was_emailed(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback_mail, "providers_for", lambda config, recipients: [])
    c = _app_client(tmp_path, owner_email="owner@example.com")
    try:
        c.post("/contact/feedback", data={"message": "Nobody will email this one."})
        page = c.get("/contact/inbox").text
        assert "Email is not set up on the server" in page
    finally:
        c.__exit__(None, None, None)
