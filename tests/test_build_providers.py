"""Tests for ragra.cli._build_providers - the one place a concrete
NotificationProvider gets constructed from Config. Hermes and email are
both optional and independently gated: neither should ever be included
unless fully configured.
"""

from pathlib import Path

from ragra.adapters.calendar import CalendarTokenPaths
from ragra.adapters.classroom import ClassroomTokenPaths
from ragra.adapters.notify import EmailProvider, HermesProvider
from ragra.cli import _build_providers
from ragra.config import Config


def _config(tmp_path: Path, **overrides) -> Config:
    defaults = dict(
        ragra_home=tmp_path,
        db_path=tmp_path / "ragra.db",
        hermes_bin=None,
        notify_target=None,
        fast_student_id=None,
        calendar_id="primary",
        calendar_paths=CalendarTokenPaths(tmp_path / "client.json", tmp_path / "token.json"),
        classroom_paths=ClassroomTokenPaths(
            tmp_path / "client.json", tmp_path / "no-such-token.json", tmp_path / "no-such-legacy-token.json"
        ),
        web_host="127.0.0.1",
        web_port=8731,
        sheets_api_key=None,
        fast_timetable_spreadsheet_id=None,
        smtp_host=None,
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_use_ssl=False,
        email_from=None,
        email_to=None,
        web_base_url=None,
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_empty_config_yields_no_providers(tmp_path):
    assert _build_providers(_config(tmp_path)) == []


def test_hermes_only_included_when_fully_configured(tmp_path):
    partial = _config(tmp_path, hermes_bin=Path("hermes"))
    assert _build_providers(partial) == []

    full = _config(tmp_path, hermes_bin=Path("hermes"), notify_target="whatsapp:123")
    providers = _build_providers(full)
    assert len(providers) == 1
    assert isinstance(providers[0], HermesProvider)


def test_email_only_included_when_fully_configured(tmp_path):
    partial = _config(tmp_path, smtp_host="smtp.example.com", email_from="ragra@example.com")
    assert _build_providers(partial) == []  # email_to missing

    full = _config(
        tmp_path,
        smtp_host="smtp.example.com",
        email_from="ragra@example.com",
        email_to="user@example.com",
    )
    providers = _build_providers(full)
    assert len(providers) == 1
    assert isinstance(providers[0], EmailProvider)


def test_both_providers_can_be_configured_simultaneously(tmp_path):
    config = _config(
        tmp_path,
        hermes_bin=Path("hermes"),
        notify_target="whatsapp:123",
        smtp_host="smtp.example.com",
        email_from="ragra@example.com",
        email_to="user@example.com",
    )
    providers = _build_providers(config)
    assert len(providers) == 2
    assert any(isinstance(p, HermesProvider) for p in providers)
    assert any(isinstance(p, EmailProvider) for p in providers)


def test_email_provider_credentials_never_stored_as_plain_config_leak(tmp_path):
    # Sanity check that the password flows through to the provider object
    # (needed for login) rather than being silently dropped - the redaction
    # guarantee is that it never appears in a NotifyResult.error, not that
    # the provider doesn't have it at all (see test_notify.py).
    config = _config(
        tmp_path,
        smtp_host="smtp.example.com",
        smtp_password="super-secret-password",
        email_from="ragra@example.com",
        email_to="user@example.com",
    )
    providers = _build_providers(config)
    email_provider = next(p for p in providers if isinstance(p, EmailProvider))
    assert email_provider.password == "super-secret-password"
