"""Building one user's notification providers.

The one place a concrete NotificationProvider is constructed. Since P3-9
that construction needs two halves: the deployment's infrastructure (which
SMTP relay, where the Hermes binary is) from Config, and this user's own
destination from the database. A channel appears only when both are
present.

The negative tests here carry the weight. The failure this design exists to
prevent is not "notifications did not work" - it is one user's academic
deadlines being delivered to another user's phone, which is exactly what a
fallback to a globally configured recipient would produce.
"""

from pathlib import Path

import pytest

from ragra.adapters.calendar import CalendarTokenPaths
from ragra.adapters.classroom import ClassroomTokenPaths
from ragra.adapters.notify import EmailProvider, HermesProvider
from ragra.cli import _build_providers
from ragra.config import Config
from ragra.notifications.preferences import (
    NotificationPreferences,
    adopt_environment_defaults,
    load_preferences,
    save_preferences,
)
from tests.support import make_user, owner_id


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


def _relay(tmp_path: Path, **overrides) -> Config:
    """A deployment with both kinds of infrastructure available, so tests
    below vary only the per-user half."""
    return _config(
        tmp_path,
        hermes_bin=Path("hermes"),
        smtp_host="smtp.example.com",
        email_from="ragra@example.com",
        **overrides,
    )


@pytest.fixture
def alice(conn) -> int:
    return owner_id(conn)


@pytest.fixture
def bea(conn) -> int:
    return make_user(conn, google_sub="providers-second")


def _set(conn, user_id, **fields):
    save_preferences(conn, user_id=user_id, preferences=NotificationPreferences(**fields))


# ---------------------------------------------------------------------------
# Nothing configured
# ---------------------------------------------------------------------------


def test_a_user_with_no_preferences_gets_no_providers(conn, tmp_path, alice):
    """The default for a new account. Silence is correct: a new user must
    not start receiving notifications at an address they never gave."""
    assert _build_providers(conn, _relay(tmp_path), user_id=alice) == []


def test_an_environment_destination_is_never_used_as_a_fallback(conn, tmp_path, alice):
    """The core isolation property of this module. If a globally configured
    recipient could stand in for a missing per-user one, every user without
    preferences would have their reminders delivered to whoever that
    address belongs to."""
    config = _relay(tmp_path, notify_target="whatsapp:owner", email_to="owner@example.com")

    assert _build_providers(conn, config, user_id=alice) == []


def test_no_infrastructure_means_no_provider_however_it_is_configured(conn, tmp_path, alice):
    """A destination alone cannot deliver anything - there has to be
    something to deliver through."""
    _set(conn, alice, email_enabled=True, email_to="a@example.com",
         hermes_enabled=True, hermes_target="whatsapp:1")

    assert _build_providers(conn, _config(tmp_path), user_id=alice) == []


# ---------------------------------------------------------------------------
# Each channel, independently gated
# ---------------------------------------------------------------------------


def test_hermes_needs_both_the_binary_and_this_users_target(conn, tmp_path, alice):
    _set(conn, alice, hermes_enabled=True, hermes_target="whatsapp:123")

    assert _build_providers(conn, _config(tmp_path), user_id=alice) == []  # no binary

    providers = _build_providers(conn, _config(tmp_path, hermes_bin=Path("hermes")), user_id=alice)
    assert len(providers) == 1
    assert isinstance(providers[0], HermesProvider)
    assert providers[0].target == "whatsapp:123"


def test_email_needs_both_the_relay_and_this_users_address(conn, tmp_path, alice):
    _set(conn, alice, email_enabled=True, email_to="user@example.com")

    partial = _config(tmp_path, smtp_host="smtp.example.com")  # no from address
    assert _build_providers(conn, partial, user_id=alice) == []

    providers = _build_providers(conn, _relay(tmp_path), user_id=alice)
    email = next(p for p in providers if isinstance(p, EmailProvider))
    assert email.to_address == "user@example.com"


def test_a_disabled_channel_keeps_its_address_but_sends_nothing(conn, tmp_path, alice):
    """Enabled and destination are separate so switching a channel off does
    not throw away the address it will be switched back on with."""
    _set(conn, alice, email_enabled=False, email_to="user@example.com")

    assert _build_providers(conn, _relay(tmp_path), user_id=alice) == []
    assert load_preferences(conn, user_id=alice).email_to == "user@example.com"


def test_both_channels_can_be_active_at_once(conn, tmp_path, alice):
    _set(conn, alice, email_enabled=True, email_to="user@example.com",
         hermes_enabled=True, hermes_target="whatsapp:123")

    providers = _build_providers(conn, _relay(tmp_path), user_id=alice)

    assert len(providers) == 2
    assert any(isinstance(p, HermesProvider) for p in providers)
    assert any(isinstance(p, EmailProvider) for p in providers)


# ---------------------------------------------------------------------------
# Two users
# ---------------------------------------------------------------------------


def test_each_user_gets_their_own_destination(conn, tmp_path, alice, bea):
    _set(conn, alice, email_enabled=True, email_to="alice@example.com")
    _set(conn, bea, email_enabled=True, email_to="bea@example.com")
    config = _relay(tmp_path)

    alice_email = next(
        p for p in _build_providers(conn, config, user_id=alice) if isinstance(p, EmailProvider)
    )
    bea_email = next(
        p for p in _build_providers(conn, config, user_id=bea) if isinstance(p, EmailProvider)
    )

    assert alice_email.to_address == "alice@example.com"
    assert bea_email.to_address == "bea@example.com"


def test_one_users_configuration_does_not_give_another_user_a_channel(conn, tmp_path, alice, bea):
    _set(conn, alice, email_enabled=True, email_to="alice@example.com",
         hermes_enabled=True, hermes_target="whatsapp:alice")

    assert _build_providers(conn, _relay(tmp_path), user_id=bea) == []


def test_deleting_a_user_removes_their_preferences(conn, alice, bea):
    _set(conn, bea, email_enabled=True, email_to="bea@example.com")

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users WHERE id = ?", (bea,))
    conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM notification_preferences WHERE user_id = ?", (bea,)
    ).fetchone()["c"]
    assert remaining == 0


# ---------------------------------------------------------------------------
# Adopting the environment's destinations, once
# ---------------------------------------------------------------------------


def test_the_existing_deployments_destinations_can_be_adopted(conn, tmp_path, alice):
    """Without this the current single user's reminders would simply stop
    being delivered the moment destinations became per-user data."""
    config = _relay(tmp_path, notify_target="whatsapp:owner", email_to="owner@example.com")

    assert adopt_environment_defaults(conn, config, user_id=alice) is True

    providers = _build_providers(conn, config, user_id=alice)
    assert len(providers) == 2


def test_adopting_never_overwrites_a_real_choice(conn, tmp_path, alice):
    """A stale environment variable must not be able to redirect a user who
    has since chosen where their reminders go."""
    _set(conn, alice, email_enabled=True, email_to="chosen@example.com")
    config = _relay(tmp_path, email_to="stale@example.com")

    assert adopt_environment_defaults(conn, config, user_id=alice) is False
    assert load_preferences(conn, user_id=alice).email_to == "chosen@example.com"


def test_adopting_nothing_is_a_safe_no_op(conn, tmp_path, alice):
    assert adopt_environment_defaults(conn, _relay(tmp_path), user_id=alice) is False
    assert load_preferences(conn, user_id=alice).any_channel_configured is False


def test_adopting_only_affects_the_account_that_asked(conn, tmp_path, alice, bea):
    config = _relay(tmp_path, email_to="owner@example.com")
    adopt_environment_defaults(conn, config, user_id=alice)

    assert load_preferences(conn, user_id=bea).email_to is None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_the_smtp_password_reaches_the_provider_and_stays_in_the_environment(
    conn, tmp_path, alice
):
    """The password is needed to log in, so it must flow through - the
    guarantee is that it never appears in a NotifyResult.error (see
    tests/test_notify.py), not that the provider does not hold it. It is
    also never written to notification_preferences: that table holds
    destinations, which are not secrets."""
    _set(conn, alice, email_enabled=True, email_to="user@example.com")
    config = _relay(tmp_path, smtp_password="super-secret-password")

    providers = _build_providers(conn, config, user_id=alice)
    email = next(p for p in providers if isinstance(p, EmailProvider))
    assert email.password == "super-secret-password"

    stored = " ".join(
        str(value)
        for row in conn.execute("SELECT * FROM notification_preferences")
        for value in tuple(row)
    )
    assert "super-secret-password" not in stored
