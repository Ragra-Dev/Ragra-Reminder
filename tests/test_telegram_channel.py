"""The Telegram channel: linking a chat to an account, the signed webhook, and
answering only the account that owns the chat."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ragra import chat
from ragra.adapters.notify import Notification, NotifyResult, TelegramProvider
from ragra.config import load_config
from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.notifications.preferences import (
    NotificationPreferences,
    channels_needing_welcome,
    load_preferences,
    save_preferences,
)
from ragra.sync.classroom_sync import sync_classroom
from ragra.web import telegram as tg
from ragra.web.app import create_app
from tests.support import make_user, owner_id
from tests.test_discovery_notification import _coursework
from tests.test_relevance_persistence import FakeClassroomClient

TOKEN = "123456:ABC-test-token"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RAGRA_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("RAGRA_TELEGRAM_BOT_USERNAME", "@RagraTestBot")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tg.db"
    with connect_closing(path):
        pass
    return path


class _Sender:
    def __init__(self):
        self.sent = []

    def send(self, notification):
        self.sent.append(notification.text)
        return NotifyResult(ok=True)


def _update(chat_id, text, chat_type="private"):
    return json.dumps({"update_id": 1, "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}).encode()


def _add_task(conn, user_id, external_id, title):
    sync_classroom(
        conn, FakeClassroomClient(coursework=[_coursework(external_id, days_out=3, title=title)]), user_id=user_id
    )


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, text="{}"):
        self.status_code, self.text = status, text


def test_the_provider_posts_to_the_bot_api(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.post", lambda url, **kw: calls.append((url, kw)) or _Resp())

    result = TelegramProvider(bot_token=TOKEN, chat_id="42").send(Notification(text="New announcement: x"))

    assert result.ok
    url, kw = calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert kw["json"]["chat_id"] == "42" and kw["json"]["text"] == "New announcement: x"


def test_a_failure_never_leaks_the_bot_token(monkeypatch):
    monkeypatch.setattr("requests.post", lambda url, **kw: _Resp(401, f"bad {TOKEN}"))

    result = TelegramProvider(bot_token=TOKEN, chat_id="42").send(Notification(text="x"))

    assert not result.ok and TOKEN not in result.error and "TELEGRAM_TRANSPORT_ERROR" in result.error


# ---------------------------------------------------------------------------
# webhook authenticity + parsing
# ---------------------------------------------------------------------------


def test_the_secret_check_fails_closed():
    good = tg.webhook_secret(TOKEN)
    assert tg.secret_is_valid(header=good, bot_token=TOKEN)
    assert not tg.secret_is_valid(header="wrong", bot_token=TOKEN)
    assert not tg.secret_is_valid(header=None, bot_token=TOKEN)
    assert not tg.secret_is_valid(header=good, bot_token=None)


def test_only_private_text_messages_are_parsed():
    assert tg.parse_update(_update(42, " pending ")) == ("42", "pending")
    assert tg.parse_update(_update(-100, "pending", chat_type="group")) is None
    assert tg.parse_update(b"not json") is None
    assert tg.parse_update(b'{"message": "junk"}') is None
    assert tg.parse_update(json.dumps({"message": {"chat": {"id": 1, "type": "private"}}}).encode()) is None


def test_slash_commands_map_to_the_same_words():
    assert tg._command_to_text("/pending@RagraBot") == "pending"
    assert tg._command_to_text("/today") == "today"
    assert tg._command_to_text("what is due") == "what is due"


@pytest.fixture
def web(db_path):
    return TestClient(create_app(db_path=db_path), client=("203.0.113.9", 5000))


def test_an_update_without_the_secret_is_refused(web, env, monkeypatch):
    seen = []
    monkeypatch.setattr(tg, "reply_to_telegram", lambda *a, **k: seen.append(a))

    resp = web.post("/telegram/webhook", content=_update(42, "pending"))
    wrong = web.post("/telegram/webhook", content=_update(42, "pending"), headers={"x-telegram-bot-api-secret-token": "nope"})

    assert resp.status_code == wrong.status_code == 403
    assert seen == []


def test_a_signed_update_is_answered_without_a_csrf_token(web, env, monkeypatch):
    seen = []
    monkeypatch.setattr(tg, "reply_to_telegram", lambda db, chat_id, text, **k: seen.append((chat_id, text)))

    resp = web.post(
        "/telegram/webhook", content=_update(42, "pending"),
        headers={"x-telegram-bot-api-secret-token": tg.webhook_secret(TOKEN)},
    )

    assert resp.status_code == 200
    assert seen == [("42", "pending")]


def test_with_no_bot_token_everything_is_refused(web, monkeypatch):
    monkeypatch.delenv("RAGRA_TELEGRAM_BOT_TOKEN", raising=False)
    assert web.post("/telegram/webhook", content=_update(1, "x"), headers={"x-telegram-bot-api-secret-token": "x"}).status_code == 403


# ---------------------------------------------------------------------------
# linking
# ---------------------------------------------------------------------------


def test_the_start_code_links_the_chat_to_the_account(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        code = repo.create_telegram_link_code(conn, user_id=uid)

    reply = tg.reply_to_telegram(db_path, "42", f"/start {code}", config=load_config(), sender=_Sender())

    assert reply.startswith(tg.CONNECTED_TEXT)
    with connect_closing(db_path) as conn:
        prefs = load_preferences(conn, user_id=uid)
    assert (prefs.telegram_enabled, prefs.telegram_chat_id) == (True, "42")


def test_a_code_works_once_only(db_path):
    with connect_closing(db_path) as conn:
        code = repo.create_telegram_link_code(conn, user_id=owner_id(conn))
    tg.reply_to_telegram(db_path, "42", f"/start {code}", config=load_config(), sender=_Sender())

    again = tg.reply_to_telegram(db_path, "99", f"/start {code}", config=load_config(), sender=_Sender())

    assert again == tg.EXPIRED_TEXT
    with connect_closing(db_path) as conn:
        assert repo.user_id_for_telegram(conn, chat_id="99") is None


def test_an_expired_code_is_refused(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        code = repo.create_telegram_link_code(conn, user_id=uid)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn.execute("UPDATE telegram_link_codes SET created_at = ?", (old,))
        conn.commit()

    assert tg.reply_to_telegram(db_path, "42", f"/start {code}", config=load_config(), sender=_Sender()) == tg.EXPIRED_TEXT


def test_a_new_code_replaces_the_old_one(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        first = repo.create_telegram_link_code(conn, user_id=uid)
        second = repo.create_telegram_link_code(conn, user_id=uid)

    assert tg.reply_to_telegram(db_path, "1", f"/start {first}", config=load_config(), sender=_Sender()) == tg.EXPIRED_TEXT
    assert tg.reply_to_telegram(db_path, "1", f"/start {second}", config=load_config(), sender=_Sender()).startswith(tg.CONNECTED_TEXT)


def test_a_chat_belongs_to_one_account_at_a_time(db_path):
    with connect_closing(db_path) as conn:
        alice, bea = owner_id(conn), make_user(conn, google_sub="bea-sub")
        repo.connect_telegram_chat(conn, user_id=alice, chat_id="42")
        repo.connect_telegram_chat(conn, user_id=bea, chat_id="42")

        assert repo.user_id_for_telegram(conn, chat_id="42") == bea
        assert load_preferences(conn, user_id=alice).telegram_chat_id is None


# ---------------------------------------------------------------------------
# answering
# ---------------------------------------------------------------------------


def test_an_unlinked_chat_gets_no_data(db_path):
    with connect_closing(db_path) as conn:
        _add_task(conn, owner_id(conn), "cw-1", "Secret Lab")

    reply = tg.reply_to_telegram(db_path, "777", "pending", config=load_config(), sender=_Sender())

    assert reply == tg.NOT_LINKED_TEXT and "Secret Lab" not in reply


def test_a_linked_chat_gets_only_its_own_work(db_path):
    with connect_closing(db_path) as conn:
        alice, bea = owner_id(conn), make_user(conn, google_sub="bea-sub")
        repo.connect_telegram_chat(conn, user_id=alice, chat_id="1")
        repo.connect_telegram_chat(conn, user_id=bea, chat_id="2")
        _add_task(conn, alice, "cw-a", "Alice Only Task")
        _add_task(conn, bea, "cw-b", "Bea Only Task")
    sender = _Sender()

    reply = tg.reply_to_telegram(db_path, "2", "/pending", config=load_config(), sender=sender)

    assert "Bea Only Task" in reply and "Alice Only Task" not in reply
    assert sender.sent == [reply]


def test_start_with_no_code_greets_a_linked_chat_and_points_an_unlinked_one(db_path):
    with connect_closing(db_path) as conn:
        repo.connect_telegram_chat(conn, user_id=owner_id(conn), chat_id="1")

    assert tg.reply_to_telegram(db_path, "1", "/start", config=load_config(), sender=_Sender()) == chat.HELP_TEXT
    assert tg.reply_to_telegram(db_path, "2", "/start", config=load_config(), sender=_Sender()) == tg.NOT_LINKED_TEXT


# ---------------------------------------------------------------------------
# the Account page
# ---------------------------------------------------------------------------


@pytest.fixture
def account(db_path):
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
    return TestClient(create_app(db_path=db_path), base_url="http://127.0.0.1", client=("127.0.0.1", 5000)), uid


def _token(client):
    return client.get("/account").text.split('name="csrf_token" value="')[1].split('"')[0]


def test_connect_sends_the_person_to_the_bot_with_a_fresh_code(account, env, db_path):
    client, uid = account

    resp = client.post("/account/telegram/link", data={"csrf_token": _token(client)}, follow_redirects=False)

    assert resp.status_code == 303
    code = resp.headers["location"].split("?start=")[1]
    assert resp.headers["location"].startswith("https://t.me/RagraTestBot?start=")
    with connect_closing(db_path) as conn:
        assert repo.consume_telegram_link_code(conn, code=code) == uid


def test_connect_without_a_bot_configured_says_so(account, monkeypatch):
    client, _ = account
    monkeypatch.delenv("RAGRA_TELEGRAM_BOT_TOKEN", raising=False)
    resp = client.post("/account/telegram/link", data={"csrf_token": _token(client)}, follow_redirects=False)
    assert resp.headers["location"] == "/account?saved=notelegram"


def test_the_form_cannot_point_alerts_at_someone_elses_chat(account, db_path):
    """The chat id comes only from the bot handshake, so a forged field is ignored."""
    client, uid = account
    with connect_closing(db_path) as conn:
        repo.connect_telegram_chat(conn, user_id=uid, chat_id="42")

    client.post(
        "/account/notifications",
        data={"csrf_token": _token(client), "telegram_enabled": "1", "telegram_chat_id": "666"},
    )

    with connect_closing(db_path) as conn:
        assert load_preferences(conn, user_id=uid).telegram_chat_id == "42"


def test_telegram_can_be_switched_off_and_disconnected(account, db_path):
    client, uid = account
    with connect_closing(db_path) as conn:
        repo.connect_telegram_chat(conn, user_id=uid, chat_id="42")

    client.post("/account/notifications", data={"csrf_token": _token(client)})  # box left unticked
    with connect_closing(db_path) as conn:
        prefs = load_preferences(conn, user_id=uid)
    assert (prefs.telegram_enabled, prefs.telegram_chat_id) == (False, "42")

    client.post("/account/telegram/disconnect", data={"csrf_token": _token(client)})
    with connect_closing(db_path) as conn:
        assert load_preferences(conn, user_id=uid).telegram_chat_id is None


def test_a_connected_account_shows_as_connected(account, db_path):
    client, uid = account
    with connect_closing(db_path) as conn:
        repo.connect_telegram_chat(conn, user_id=uid, chat_id="42")
    page = client.get("/account").text
    # Telegram is shown as "Under development" and disabled in the profile page for now.
    assert "Telegram" in page and "Under development" in page
    assert 'id="telegram_enabled_ui" disabled' in page


def test_alerts_go_to_telegram_once_linked(db_path, monkeypatch):
    from ragra.notifications.preferences import providers_for

    monkeypatch.setenv("RAGRA_TELEGRAM_BOT_TOKEN", TOKEN)
    with connect_closing(db_path) as conn:
        uid = owner_id(conn)
        repo.connect_telegram_chat(conn, user_id=uid, chat_id="42")
        providers = providers_for(conn, load_config(), user_id=uid)
    assert [type(p).__name__ for p in providers] == ["TelegramProvider"]


def test_telegram_welcome_only_when_switched_on_or_moved():
    off = NotificationPreferences(telegram_chat_id="1")
    on = NotificationPreferences(telegram_enabled=True, telegram_chat_id="1")
    assert channels_needing_welcome(off, on) == ["telegram"]
    assert channels_needing_welcome(on, on) == []
