"""Per-user notification preferences, and building that user's providers.

The split this module exists to enforce: *infrastructure* is a property of
the deployment and lives in the environment (which SMTP relay, where the
optional Hermes binary is); *destination* is a property of a person and
lives in the database (which address, which messaging target).

Before P3, both came from the environment, which is correct for one user
and dangerous for several - the failure is not a broken feature, it is one
user's deadlines arriving on another user's phone. So `providers_for` takes
a user id and never falls back to a global destination when that user has
none configured. An empty provider list is a normal, fully supported state
everywhere in Ragra: reminders stay PENDING rather than being sent
somewhere they do not belong.

Nothing stored here is a secret. An address is not a credential, and SMTP
passwords stay in the environment - which is why this table needs no
encryption while google_credentials does.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ragra.adapters.notify import EmailProvider, HermesProvider, NotificationProvider
from ragra.config import Config
from ragra.db.repo import now_iso


@dataclass(frozen=True)
class NotificationPreferences:
    """One user's delivery destinations. `enabled` and the destination are
    separate so a user can switch a channel off without losing the address
    they will switch back on."""

    email_enabled: bool = False
    email_to: str | None = None
    hermes_enabled: bool = False
    hermes_target: str | None = None

    @property
    def any_channel_configured(self) -> bool:
        return bool(
            (self.email_enabled and self.email_to)
            or (self.hermes_enabled and self.hermes_target)
        )


def load_preferences(conn: sqlite3.Connection, *, user_id: int) -> NotificationPreferences:
    """This user's preferences, or the all-off default.

    A missing row is not an error: "no delivery configured" is a supported
    state, and it is the correct default for a new account - a new user
    must not start receiving notifications at an address they never gave.
    """
    row = conn.execute(
        "SELECT * FROM notification_preferences WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return NotificationPreferences()
    return NotificationPreferences(
        email_enabled=bool(row["email_enabled"]),
        email_to=row["email_to"],
        hermes_enabled=bool(row["hermes_enabled"]),
        hermes_target=row["hermes_target"],
    )


def save_preferences(
    conn: sqlite3.Connection, *, user_id: int, preferences: NotificationPreferences
) -> None:
    now = now_iso()
    conn.execute(
        """INSERT INTO notification_preferences
             (user_id, email_enabled, email_to, hermes_enabled, hermes_target,
              created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             email_enabled = excluded.email_enabled,
             email_to = excluded.email_to,
             hermes_enabled = excluded.hermes_enabled,
             hermes_target = excluded.hermes_target,
             updated_at = excluded.updated_at""",
        (
            user_id,
            1 if preferences.email_enabled else 0,
            preferences.email_to,
            1 if preferences.hermes_enabled else 0,
            preferences.hermes_target,
            now,
            now,
        ),
    )
    conn.commit()


def providers_for(
    conn: sqlite3.Connection, config: Config, *, user_id: int
) -> list[NotificationProvider]:
    """Build the notification providers for one user.

    A channel is included only when both halves are present: the
    deployment's infrastructure for it, and this user's own destination.
    There is deliberately no fallback to a globally configured recipient -
    that fallback is precisely how a second user's reminders would be
    delivered to the first user.
    """
    providers: list[NotificationProvider] = []
    preferences = load_preferences(conn, user_id=user_id)

    if preferences.hermes_enabled and preferences.hermes_target and config.hermes_bin:
        providers.append(
            HermesProvider(hermes_bin=config.hermes_bin, target=preferences.hermes_target)
        )

    if (
        preferences.email_enabled
        and preferences.email_to
        and config.smtp_host
        and config.email_from
    ):
        providers.append(
            EmailProvider(
                host=config.smtp_host,
                port=config.smtp_port,
                from_address=config.email_from,
                to_address=preferences.email_to,
                username=config.smtp_username,
                password=config.smtp_password,
                use_ssl=config.smtp_use_ssl,
                base_url=config.web_base_url,
            )
        )

    return providers


def adopt_environment_defaults(
    conn: sqlite3.Connection, config: Config, *, user_id: int
) -> bool:
    """Move the deployment's environment-configured destinations onto one
    user, once.

    This is the migration path for the existing single-user deployment: the
    owner's WhatsApp target and email address are currently in the
    environment, and without this their reminders would simply stop being
    delivered the moment destinations became per-user.

    Returns False if the user already has preferences, so this is safe to
    call unconditionally and can never overwrite a real choice with a stale
    environment variable.
    """
    existing = conn.execute(
        "SELECT 1 FROM notification_preferences WHERE user_id = ?", (user_id,)
    ).fetchone()
    if existing is not None:
        return False

    if not config.notify_target and not config.email_to:
        return False

    save_preferences(
        conn,
        user_id=user_id,
        preferences=NotificationPreferences(
            email_enabled=bool(config.email_to),
            email_to=config.email_to,
            hermes_enabled=bool(config.notify_target),
            hermes_target=config.notify_target,
        ),
    )
    return True


def describe(preferences: NotificationPreferences) -> dict[str, str]:
    """A safe-to-print summary. Destinations are shown - they are not
    secrets, and hiding them would make this useless for the one question
    it answers, "where are my reminders going?"."""
    return {
        "email": (
            f"on -> {preferences.email_to}"
            if preferences.email_enabled and preferences.email_to
            else "off"
        ),
        "hermes": (
            f"on -> {preferences.hermes_target}"
            if preferences.hermes_enabled and preferences.hermes_target
            else "off"
        ),
    }
