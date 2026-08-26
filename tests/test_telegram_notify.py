"""Tests for the remote-capable Telegram notification adapter. No real
network calls - urllib.request.urlopen is monkeypatched throughout.
"""

import io
import json
import urllib.error

import pytest

from ragra.adapters.telegram_notify import send_telegram_notification


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_not_configured_without_token_or_chat_id():
    result = send_telegram_notification(bot_token=None, chat_id="123", message="hi")
    assert not result.ok
    assert result.error == "notification delivery is not configured"

    result = send_telegram_notification(bot_token="abc", chat_id=None, message="hi")
    assert not result.ok


def test_successful_send(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _FakeResponse({"ok": True, "result": {}})
    )
    result = send_telegram_notification(bot_token="fake-token", chat_id="123", message="hello")
    assert result.ok
    assert result.error is None


def test_telegram_api_reports_failure(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse({"ok": False, "description": "chat not found"}),
    )
    result = send_telegram_notification(bot_token="fake-token", chat_id="bad-id", message="hello")
    assert not result.ok
    assert result.error == "chat not found"


def test_http_error_is_caught_and_reported(monkeypatch):
    def raise_http_error(req, timeout=None):
        body = io.BytesIO(json.dumps({"description": "Unauthorized"}).encode("utf-8"))
        raise urllib.error.HTTPError(url="x", code=401, msg="Unauthorized", hdrs=None, fp=body)

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)
    result = send_telegram_notification(bot_token="bad-token", chat_id="123", message="hello")
    assert not result.ok
    assert "Unauthorized" in result.error


def test_network_failure_is_caught_and_reported(monkeypatch):
    def raise_network_error(req, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", raise_network_error)
    result = send_telegram_notification(bot_token="tok", chat_id="123", message="hello")
    assert not result.ok
    assert "network unreachable" in result.error


def test_bot_token_never_appears_in_the_result_on_failure(monkeypatch):
    def raise_network_error(req, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", raise_network_error)
    result = send_telegram_notification(bot_token="super-secret-token-value", chat_id="123", message="hi")
    assert "super-secret-token-value" not in (result.error or "")
