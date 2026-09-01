"""Tests for the Notification value object (ragra.adapters.notify) and
HermesProvider's adaptation to it. EmailProvider has its own test file
(test_email_provider.py).
"""

from pathlib import Path

from ragra.adapters.notify import HermesProvider, Notification, NotifyResult


def test_hermes_provider_extracts_notification_text(monkeypatch):
    captured = {}

    def fake_send_notification(*, hermes_bin, target, message):
        captured["message"] = message
        return NotifyResult(ok=True)

    monkeypatch.setattr("ragra.adapters.notify.send_notification", fake_send_notification)

    provider = HermesProvider(hermes_bin=Path("hermes"), target="whatsapp:123")
    result = provider.send(Notification(text="hello from hermes test", reminder_id=42, category="T_MINUS_1D"))

    assert result.ok is True
    assert captured["message"] == "hello from hermes test"
