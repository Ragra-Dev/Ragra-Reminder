"""The hosted WhatsApp channel, the chat assistant behind it, and the
new-announcement alert.

What matters most here is who can be answered and with what: an unsigned
webhook call is refused, a number nobody owns gets no data, and a student only
ever hears about their own section's work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ragra import chat
from ragra.adapters.notify import (
    Notification,
    NotifyResult,
    WhatsAppCloudProvider,
    normalize_whatsapp_number,
)
from ragra.config import load_config
from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.notifications.preferences import (
    NotificationPreferences,
    channels_needing_welcome,
    save_preferences,
)
from ragra.reminders.dispatch import dispatch_due_reminders
from ragra.sync.classroom_sync import sync_classroom
from ragra.web import whatsapp as wa
from ragra.web.app import create_app
from tests.support import make_user, owner_id, sign_in
from tests.test_discovery_notification import AnnouncementOnlyClient, _coursework
from tests.test_relevance_persistence import FakeClassroomClient, _item

# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+92 300 1234567", "923001234567"),
        ("0092-300-1234567", "923001234567"),
        ("923001234567", "923001234567"),
        ("12345", None),
        ("not a number", None),
        ("", None),
        (None, None),
    ],
)
def test_numbers_are_normalised_to_digits_or_rejected(raw, expected):
    assert normalize_whatsapp_number(raw) == expected


# ---------------------------------------------------------------------------
# the provider
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_the_provider_posts_a_text_message_to_the_cloud_api(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.post", lambda url, **kw: calls.append((url, kw)) or _Resp())

    result = WhatsAppCloudProvider(token="TOK", phone_number_id="555", to_number="923001234567").send(
        Notification(text="New announcement: CS: hello")
    )

    assert result.ok
    url, kw = calls[0]
    assert url.endswith("/555/messages")
    assert kw["headers"]["Authorization"] == "Bearer TOK"
    assert kw["json"]["to"] == "923001234567"
    assert kw["json"]["text"]["body"] == "New announcement: CS: hello"


def test_a_closed_24h_window_falls_back_to_the_template(monkeypatch):
    sent = []

    def fake_post(url, **kw):
        sent.append(kw["json"])
        if kw["json"]["type"] == "text":
            return _Resp(400, {"error": {"code": 131047}})
        return _Resp()

    monkeypatch.setattr("requests.post", fake_post)

    result = WhatsAppCloudProvider(
        token="T", phone_number_id="1", to_number="923001234567", template="ragra_alert"
    ).send(Notification(text="Due today: CS: Lab"))

    assert result.ok
    assert [m["type"] for m in sent] == ["text", "template"]
    assert sent[1]["template"]["components"][0]["parameters"][0]["text"] == "Due today: CS: Lab"


def test_a_failure_is_reported_without_leaking_the_token(monkeypatch):
    monkeypatch.setattr("requests.post", lambda url, **kw: _Resp(401, {"error": {"message": "bad TOKENVALUE"}}))

    result = WhatsAppCloudProvider(token="TOKENVALUE", phone_number_id="1", to_number="923001234567").send(
        Notification(text="x")
    )

    assert not result.ok
    assert "WHATSAPP_TRANSPORT_ERROR" in result.error
    assert "TOKENVALUE" not in result.error


# ---------------------------------------------------------------------------
# signature + payload
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_check():
    body = b'{"a":1}'
    assert wa.signature_is_valid(body=body, header=_sign(body, "s3"), secret="s3")
    assert not wa.signature_is_valid(body=body, header=_sign(body, "other"), secret="s3")
    assert not wa.signature_is_valid(body=body, header=None, secret="s3")
    assert not wa.signature_is_valid(body=body, header=_sign(body, "s3"), secret=None)  # fails closed


def _payload(*messages):
    return {"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}


def test_only_text_messages_are_extracted():
    payload = _payload(
        {"from": "923001234567", "type": "text", "text": {"body": "pending"}},
        {"from": "923001234567", "type": "image"},
        {"from": "923001234567", "type": "text", "text": {"body": "   "}},
    )
    assert wa.extract_text_messages(payload) == [("923001234567", "pending")]
    assert wa.extract_text_messages(None) == []
    assert wa.extract_text_messages({"entry": "junk"}) == []


# ---------------------------------------------------------------------------
# the webhook routes
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RAGRA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("RAGRA_WHATSAPP_APP_SECRET", "app-secret")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "wa.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def web(db_path):
    return TestClient(create_app(db_path=db_path), client=("203.0.113.9", 5000))


def test_the_handshake_echoes_the_challenge_for_the_right_token(web, env):
    ok = web.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"})
    assert (ok.status_code, ok.text) == (200, "1234")

    bad = web.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1234"})
    assert bad.status_code == 403


def test_the_handshake_is_refused_when_no_token_is_configured(web, monkeypatch):
    monkeypatch.delenv("RAGRA_WHATSAPP_VERIFY_TOKEN", raising=False)
    resp = web.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"})
    assert resp.status_code == 403


def test_an_unsigned_message_is_refused_and_never_processed(web, env, monkeypatch):
    seen = []
    monkeypatch.setattr(wa, "reply_to_whatsapp", lambda *a, **k: seen.append(a))
    body = json.dumps(_payload({"from": "923001234567", "type": "text", "text": {"body": "pending"}})).encode()

    no_sig = web.post("/whatsapp/webhook", content=body)
    wrong = web.post("/whatsapp/webhook", content=body, headers={"x-hub-signature-256": _sign(body, "wrong")})

    assert no_sig.status_code == wrong.status_code == 403
    assert seen == []


def test_a_signed_message_is_answered_without_a_csrf_token(web, env, monkeypatch):
    seen = []
    monkeypatch.setattr(wa, "reply_to_whatsapp", lambda db, number, text, **k: seen.append((number, text)))
    body = json.dumps(_payload({"from": "923001234567", "type": "text", "text": {"body": "pending"}})).encode()

    resp = web.post(
        "/whatsapp/webhook", content=body,
        headers={"x-hub-signature-256": _sign(body, "app-secret"), "content-type": "application/json"},
    )

    assert resp.status_code == 200
    assert seen == [("923001234567", "pending")]


# ---------------------------------------------------------------------------
# who is answered, and with what
# ---------------------------------------------------------------------------


class _Sender:
    def __init__(self):
        self.sent = []

    def send(self, notification):
        self.sent.append(notification.text)
        return NotifyResult(ok=True)


def _link(conn, user_id, number="923001234567"):
    save_preferences(
        conn, user_id=user_id,
        preferences=NotificationPreferences(whatsapp_enabled=True, whatsapp_to=number),
    )


def _add_task(conn, user_id, external_id, title, *, days_out=3):
    sync_classroom(conn, FakeClassroomClient(coursework=[_coursework(external_id, days_out=days_out, title=title)]), user_id=user_id)


def test_a_number_nobody_owns_is_pointed_at_the_account_page_and_gets_no_data(db_path):
    with connect_closing(db_path) as conn:
        _add_task(conn, owner_id(conn), "cw-1", "Secret Lab")
    sender = _Sender()

    reply = wa.reply_to_whatsapp(db_path, "923009999999", "pending", config=load_config(), sender=sender)

    assert "isn't linked" in reply
    assert "Secret Lab" not in reply
    assert sender.sent == [reply]


def test_a_linked_number_gets_its_own_pending_work(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        _link(conn, uid)
        _add_task(conn, uid, "cw-1", "Digital Logic Lab")
    sender = _Sender()

    reply = wa.reply_to_whatsapp(db_path, "923001234567", "pending", config=load_config(), sender=sender)

    assert "Digital Logic Lab" in reply
    assert sender.sent == [reply]


def test_one_students_work_never_reaches_another(db_path):
    with connect_closing(db_path) as conn:
        alice = owner_id(conn)
        bea = make_user(conn, google_sub="bea-sub")
        _link(conn, alice, "923001111111")
        _link(conn, bea, "923002222222")
        _add_task(conn, alice, "cw-a", "Alice Only Task")
        _add_task(conn, bea, "cw-b", "Bea Only Task")

    to_bea = wa.reply_to_whatsapp(db_path, "923002222222", "pending", config=load_config(), sender=_Sender())

    assert "Bea Only Task" in to_bea
    assert "Alice Only Task" not in to_bea


def test_work_for_another_section_is_left_out(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        _link(conn, uid)
        sync_classroom(
            conn,
            FakeClassroomClient(coursework=[
                _coursework("cw-mine", days_out=3, title="My Section Task"),
                _coursework("cw-other", days_out=3, title="Other Section Task"),
            ]),
            user_id=uid,
        )
        other = conn.execute("SELECT id FROM tasks WHERE external_id = 'cw-other'").fetchone()["id"]
        repo.set_task_relevance(conn, user_id=uid, task_id=other, relevance="other_section", reason="test")

        reply = chat.answer(conn, load_config(), user_id=uid, text="pending")

    assert "My Section Task" in reply
    assert "Other Section Task" not in reply


def test_commands_need_no_ai(db_path, monkeypatch):
    monkeypatch.setattr(chat, "ask_gemini", lambda *a, **k: pytest.fail("the AI must not be called"))
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        assert "Ragra" in chat.answer(conn, load_config(), user_id=uid, text="hi")
        assert chat.answer(conn, load_config(), user_id=uid, text="Overdue?") == "Nothing overdue."
        assert chat.answer(conn, load_config(), user_id=uid, text="news") == "No new announcements."


def test_a_free_question_goes_to_the_model_with_only_this_users_snapshot(db_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(chat, "ask_gemini", lambda key, model, prompt, **k: captured.update(prompt=prompt) or "Do the lab first.")
    with connect_closing(db_path) as conn:
        alice = owner_id(conn)
        bea = make_user(conn, google_sub="bea-sub")
        _add_task(conn, alice, "cw-a", "Alice Lab")
        _add_task(conn, bea, "cw-b", "Bea Lab")
        config = replace(load_config(), gemini_api_key="k")

        reply = chat.answer(conn, config, user_id=alice, text="what should I do first?")

    assert reply == "Do the lab first."
    assert "Alice Lab" in captured["prompt"]
    assert "Bea Lab" not in captured["prompt"]
    assert "Never invent" in captured["prompt"]


def test_without_an_ai_key_a_free_question_gets_the_help_text(db_path):
    with connect_closing(db_path) as conn:
        config = replace(load_config(), gemini_api_key=None)
        assert chat.answer(conn, config, user_id=owner_id(conn), text="explain quantum physics") == chat.HELP_TEXT


def test_an_ai_failure_degrades_to_a_friendly_message(db_path, monkeypatch):
    from ragra.adapters.ai import AIAdapterError

    def boom(*a, **k):
        raise AIAdapterError("down")

    monkeypatch.setattr(chat, "ask_gemini", boom)
    with connect_closing(db_path) as conn:
        config = replace(load_config(), gemini_api_key="k")
        reply = chat.answer(conn, config, user_id=owner_id(conn), text="what now?")
    assert "couldn't think of an answer" in reply


# ---------------------------------------------------------------------------
# new-announcement alerts
# ---------------------------------------------------------------------------


def _announcement(external_id, *, age=timedelta(hours=1), text="Quiz moved to Friday"):
    created = (datetime.now(timezone.utc) - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = _item(external_id, text)
    item["creationTime"] = created
    return item


def _alerts(conn):
    return conn.execute("SELECT * FROM reminders WHERE reminder_type = 'NEW_ANNOUNCEMENT'").fetchall()


def test_a_new_recent_announcement_queues_one_alert(conn):
    client = AnnouncementOnlyClient([_announcement("an-1")])
    for _ in range(3):  # re-syncing must never alert twice
        sync_classroom(conn, client, user_id=owner_id(conn))

    assert len(_alerts(conn)) == 1


def test_an_old_announcement_is_backlog_not_news(conn):
    sync_classroom(conn, AnnouncementOnlyClient([_announcement("an-old", age=timedelta(days=30))]), user_id=owner_id(conn))
    assert _alerts(conn) == []


def test_the_alert_reads_as_a_new_announcement_and_is_delivered(conn):
    uid = owner_id(conn)
    sync_classroom(conn, AnnouncementOnlyClient([_announcement("an-1")]), user_id=uid)
    sender = _Sender()

    summary = dispatch_due_reminders(conn, user_id=uid, providers=[sender], now=datetime.now(timezone.utc).isoformat())

    assert summary.sent == 1
    assert sender.sent[0].startswith("New announcement:")
    assert "Quiz moved to Friday" in sender.sent[0]


def test_an_announcement_for_another_section_is_not_sent(conn):
    uid = owner_id(conn)
    sync_classroom(conn, AnnouncementOnlyClient([_announcement("an-1")]), user_id=uid)
    task_id = conn.execute("SELECT id FROM tasks WHERE external_id = 'an-1'").fetchone()["id"]
    repo.set_task_relevance(conn, user_id=uid, task_id=task_id, relevance="other_section", reason="test")
    sender = _Sender()

    summary = dispatch_due_reminders(conn, user_id=uid, providers=[sender], now=datetime.now(timezone.utc).isoformat())

    assert summary.sent == 0
    assert sender.sent == []


# ---------------------------------------------------------------------------
# the Account page
# ---------------------------------------------------------------------------


@pytest.fixture
def account(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
    return TestClient(create_app(db_path=db_path), base_url="http://127.0.0.1", client=("127.0.0.1", 5000)), uid


def _post(client, **data):
    token = client.get("/account").text.split('name="csrf_token" value="')[1].split('"')[0]
    return client.post("/account/notifications", data={"csrf_token": token, **data}, follow_redirects=False)


def test_a_whatsapp_number_is_saved_as_digits_and_shown_as_saved(account, db_path):
    client, uid = account

    assert _post(client, whatsapp_enabled="1", whatsapp_to="+92 300 1234567").status_code == 303

    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT * FROM notification_preferences WHERE user_id = ?", (uid,)).fetchone()
    assert (row["whatsapp_enabled"], row["whatsapp_to"]) == (1, "923001234567")
    page = client.get("/account").text
    # WhatsApp is switched off in the profile page for now: it shows its name and
    # "Under development", and carries the saved number along without editing it.
    assert "WhatsApp" in page and "Under development" in page
    assert 'id="whatsapp_enabled_ui" disabled' in page
    assert 'name="whatsapp_to" value="923001234567"' in page


def test_a_bad_number_is_refused_and_the_old_one_kept(account, db_path):
    client, uid = account
    _post(client, whatsapp_enabled="1", whatsapp_to="+92 300 1234567")

    resp = _post(client, whatsapp_enabled="1", whatsapp_to="923001234567", whatsapp_change="12")

    assert resp.headers["location"] == "/account?saved=badnumber"
    with connect_closing(db_path) as conn:
        row = conn.execute("SELECT whatsapp_to FROM notification_preferences WHERE user_id = ?", (uid,)).fetchone()
    assert row["whatsapp_to"] == "923001234567"


def test_whatsapp_gets_a_welcome_when_first_switched_on_or_changed():
    off = NotificationPreferences()
    on = NotificationPreferences(whatsapp_enabled=True, whatsapp_to="923001234567")
    moved = NotificationPreferences(whatsapp_enabled=True, whatsapp_to="923009999999")

    assert channels_needing_welcome(off, on) == ["whatsapp"]
    assert channels_needing_welcome(on, on) == []
    assert channels_needing_welcome(on, moved) == ["whatsapp"]


def test_a_new_class_material_also_alerts(conn):
    class MaterialClient(FakeClassroomClient):
        def list_course_materials(self, course_id):
            item = _announcement("mat-1", text="Lecture 5 slides")
            return [item]

    uid = owner_id(conn)
    sync_classroom(conn, MaterialClient(), user_id=uid)
    sender = _Sender()

    dispatch_due_reminders(conn, user_id=uid, providers=[sender], now=datetime.now(timezone.utc).isoformat())

    assert [m for m in sender.sent if m.startswith("New class material:")]
