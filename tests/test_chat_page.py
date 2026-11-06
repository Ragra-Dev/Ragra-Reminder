"""The in-app Agent chat: sending, replies, alerts landing in the thread, and
the isolation between accounts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.web import csrf
from ragra.web.app import create_app
from tests.support import make_user, owner_id, sign_in


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "chat.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def client(db_path):
    c = TestClient(create_app(db_path), follow_redirects=False)
    sign_in(c, db_path)
    return c


def _token(client) -> str:
    return client.headers[csrf.HEADER_NAME]


def test_chat_requires_sign_in(db_path):
    anon = TestClient(create_app(db_path), follow_redirects=False)
    assert anon.get("/chat").status_code in (302, 303, 401)
    assert anon.get("/chat/messages").status_code in (302, 303, 401)


def test_first_visit_greets_with_help(client):
    page = client.get("/chat")
    assert page.status_code == 200
    assert "pending" in page.text
    assert "Agent" in page.text


def test_sending_stores_the_message_and_a_reply(client, db_path):
    response = client.post(
        "/chat/send", data={"text": "pending"}, headers={"accept": "application/json"}
    )
    assert response.status_code == 200
    roles = [m["role"] for m in response.json()["messages"]]
    assert roles == ["user", "bot"]
    with connect_closing(db_path) as conn:
        texts = [r["text"] for r in repo.chat_messages(conn, user_id=owner_id(conn))]
    assert "pending" in texts


def test_plain_form_post_redirects_back(client):
    response = client.post("/chat/send", data={"text": "today"})
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"


def test_empty_message_is_ignored(client, db_path):
    client.post("/chat/send", data={"text": "   "})
    with connect_closing(db_path) as conn:
        assert repo.chat_messages(conn, user_id=owner_id(conn)) == []


def test_sending_needs_a_csrf_token(client):
    client.headers.pop(csrf.HEADER_NAME, None)
    assert client.post("/chat/send", data={"text": "pending"}).status_code == 403


def test_polling_returns_only_newer_messages(client, db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        first = repo.add_chat_message(conn, user_id=uid, role="alert", text="New announcement")
        repo.add_chat_message(conn, user_id=uid, role="alert", text="New assignment")
    body = client.get(f"/chat/messages?after={first}").json()
    assert [m["text"] for m in body["messages"]] == ["New assignment"]


def test_threads_are_private_to_each_account(client, db_path):
    with connect_closing(db_path) as conn:
        bea = make_user(conn, google_sub="chat-bea", display_name="Bea")
        repo.add_chat_message(conn, user_id=bea, role="alert", text="Bea's secret alert")
    assert "Bea's secret alert" not in client.get("/chat").text
    assert "Bea's secret alert" not in client.get("/chat/messages").text


def test_clear_empties_only_my_thread(client, db_path):
    with connect_closing(db_path) as conn:
        bea = make_user(conn, google_sub="chat-bea2", display_name="Bea")
        repo.add_chat_message(conn, user_id=bea, role="bot", text="keep me")
        repo.add_chat_message(conn, user_id=owner_id(conn), role="bot", text="mine")
    assert client.post("/chat/clear").status_code == 303
    with connect_closing(db_path) as conn:
        assert repo.chat_messages(conn, user_id=owner_id(conn)) == []
        assert len(repo.chat_messages(conn, user_id=bea)) == 1


def test_thread_is_trimmed(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        for i in range(repo._CHAT_KEEP + 15):
            repo.add_chat_message(conn, user_id=uid, role="bot", text=f"m{i}")
        rows = repo.chat_messages(conn, user_id=uid, limit=1000)
    assert len(rows) == repo._CHAT_KEEP
    assert rows[-1]["text"] == f"m{repo._CHAT_KEEP + 14}"


# ---------------------------------------------------------------------------
# Repeat inside the app until "Received"
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from ragra.reminders.nags import NAG_INTERVAL, run_nags  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _alert(conn, uid):
    return repo.add_chat_message(
        conn, user_id=uid, role="alert", text="Deadline reached: CS101: Lab",
        next_nag_at=NOW.isoformat(),
    )


def _alerts(conn, uid):
    return [m for m in repo.chat_messages(conn, user_id=uid) if m["role"] == "alert"]


def test_an_unacknowledged_alert_is_repeated_as_a_new_chat_message(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        _alert(conn, uid)
        assert run_nags(conn, user_id=uid, now=NOW) == 1
        assert len(_alerts(conn, uid)) == 2
        assert "Deadline reached" in _alerts(conn, uid)[-1]["text"]
        # not due again until another interval has passed
        assert run_nags(conn, user_id=uid, now=NOW + timedelta(minutes=1)) == 0
        assert run_nags(conn, user_id=uid, now=NOW + NAG_INTERVAL) == 1


def test_repeats_never_stop_on_their_own(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        _alert(conn, uid)
        moment = NOW
        for _ in range(60):
            assert run_nags(conn, user_id=uid, now=moment) == 1
            moment += NAG_INTERVAL


def test_repeats_are_not_sent_to_any_channel(db_path):
    # run_nags has no provider argument at all: repeats stay in the app.
    import inspect

    assert "providers" not in inspect.signature(run_nags).parameters


def test_pressing_received_on_a_repeat_stops_the_whole_chain(client, db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        first = _alert(conn, uid)
        run_nags(conn, user_id=uid, now=NOW)
        repeat = _alerts(conn, uid)[-1]["id"]
    assert repeat != first
    response = client.post(f"/chat/{repeat}/ack", headers={"accept": "application/json"})
    assert response.status_code == 200
    with connect_closing(db_path) as conn:
        assert all(m["acked_at"] for m in _alerts(conn, uid))
        assert run_nags(conn, user_id=uid, now=NOW + NAG_INTERVAL * 3) == 0
        assert len(_alerts(conn, uid)) == 2
    assert "Received" in client.get("/chat").text


def test_someone_elses_alert_cannot_be_acknowledged(client, db_path):
    with connect_closing(db_path) as conn:
        bea = make_user(conn, google_sub="nag-bea", display_name="Bea")
        mid = _alert(conn, bea)
    client.post(f"/chat/{mid}/ack", headers={"accept": "application/json"})
    with connect_closing(db_path) as conn:
        assert len(repo.due_nags(conn, user_id=bea, now=NOW.isoformat())) == 1
