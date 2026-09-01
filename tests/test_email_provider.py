"""Tests for EmailProvider (ragra.adapters.notify). Stubs smtplib.SMTP/
SMTP_SSL directly (no real network, no third-party fake SMTP server
dependency) so they can assert exactly what was sent - subject, body, deep
link, TLS/login behavior, and redaction on failure - without ever touching
a real mail server or real credentials.
"""

import smtplib

import pytest

from ragra.adapters.notify import EmailProvider, Notification


class _FakeSMTP:
    """Stand-in for smtplib.SMTP / smtplib.SMTP_SSL. `fail_with`, if set,
    makes send_message raise that exception instead of recording the
    message - simulating a genuine delivery failure."""

    last_instance: "_FakeSMTP | None" = None
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_calls: list[tuple[str, str]] = []
        self.sent_messages: list = []
        _FakeSMTP.last_instance = self

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_calls.append((username, password))

    def send_message(self, message):
        if _FakeSMTP.fail_with is not None:
            raise _FakeSMTP.fail_with
        self.sent_messages.append(message)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSMTP.last_instance = None
    _FakeSMTP.fail_with = None
    yield
    _FakeSMTP.last_instance = None
    _FakeSMTP.fail_with = None


@pytest.fixture
def fake_smtp(monkeypatch):
    monkeypatch.setattr("ragra.adapters.notify.smtplib.SMTP", _FakeSMTP)
    monkeypatch.setattr("ragra.adapters.notify.smtplib.SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def _provider(**overrides) -> EmailProvider:
    defaults = dict(
        host="smtp.example.com",
        port=587,
        from_address="ragra@example.com",
        to_address="user@example.com",
        username="ragra@example.com",
        password="super-secret-password",
    )
    defaults.update(overrides)
    return EmailProvider(**defaults)


def test_email_provider_sends_subject_and_body(fake_smtp):
    provider = _provider()
    notification = Notification(text="Assignment 2 is due today.", category="DUE_TODAY")

    result = provider.send(notification)

    assert result.ok is True
    sent = fake_smtp.last_instance.sent_messages[0]
    assert "DUE_TODAY" in sent["Subject"]
    assert "Assignment 2 is due today." in sent.get_content()


def test_email_provider_includes_deep_link_when_base_url_configured(fake_smtp):
    provider = _provider(base_url="http://127.0.0.1:8731")
    result = provider.send(Notification(text="Assignment 2 is due today."))

    assert result.ok is True
    sent = fake_smtp.last_instance.sent_messages[0]
    assert "http://127.0.0.1:8731" in sent.get_content()


def test_email_provider_omits_deep_link_when_not_configured(fake_smtp):
    provider = _provider(base_url=None)
    provider.send(Notification(text="Assignment 2 is due today."))

    sent = fake_smtp.last_instance.sent_messages[0]
    assert "http://" not in sent.get_content()


def test_email_provider_uses_starttls_by_default(fake_smtp):
    provider = _provider()
    provider.send(Notification(text="hello"))

    assert fake_smtp.last_instance.started_tls is True


def test_email_provider_uses_ssl_when_configured(fake_smtp):
    provider = _provider(use_ssl=True)
    provider.send(Notification(text="hello"))

    # SMTP_SSL is used directly - no separate starttls() call.
    assert fake_smtp.last_instance.started_tls is False


def test_email_provider_logs_in_with_configured_credentials(fake_smtp):
    provider = _provider(username="ragra@example.com", password="super-secret-password")
    provider.send(Notification(text="hello"))

    assert fake_smtp.last_instance.login_calls == [("ragra@example.com", "super-secret-password")]


def test_email_provider_skips_login_when_no_credentials_configured(fake_smtp):
    provider = _provider(username=None, password=None)
    provider.send(Notification(text="hello"))

    assert fake_smtp.last_instance.login_calls == []


def test_email_provider_failure_is_reported_not_raised(fake_smtp):
    _FakeSMTP.fail_with = smtplib.SMTPException("mailbox unavailable")
    provider = _provider()

    result = provider.send(Notification(text="hello"))

    assert result.ok is False
    assert "mailbox unavailable" in result.error


def test_email_provider_never_leaks_password_in_error(fake_smtp):
    secret = "super-secret-password"
    _FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, f"auth failed for {secret}".encode())
    provider = _provider(password=secret)

    result = provider.send(Notification(text="hello"))

    assert result.ok is False
    assert secret not in result.error
    assert "***" in result.error


def test_email_provider_survives_connection_error(fake_smtp, monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("ragra.adapters.notify.smtplib.SMTP", _raise)
    provider = _provider()

    result = provider.send(Notification(text="hello"))

    assert result.ok is False
    assert "connection refused" in result.error
