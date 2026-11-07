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
    assert "Your message was sent" in client.get("/contact?sent=1").text

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
