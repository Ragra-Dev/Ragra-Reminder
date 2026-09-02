"""Notification layer: a small, provider-neutral boundary between the
reminder engine and however a message actually gets delivered.

ragra/reminders/dispatch.py and ragra/health.py depend only on the
NotificationProvider protocol below (`send(notification) -> NotifyResult`) -
they never import or know about Hermes, WhatsApp, Web Push, or email
specifically. HermesProvider and EmailProvider are the concrete
implementations that ship today. HermesProvider wraps Hermes' `hermes send`
CLI as a pure process-boundary call - Ragra never imports Hermes'
messaging/gateway internals, so a broken or upgraded Hermes install cannot
corrupt Ragra state or break Ragra's imports. EmailProvider speaks SMTP
directly via the standard library. Future providers (Web Push) implement the
same protocol and get added to ragra/cli.py's _build_providers() only - the
reminder/health code that calls them never changes, and never needs to know
which providers exist.

Idempotency is the caller's responsibility (see ragra/reminders/dispatch.py):
a provider only performs a single delivery attempt and reports success or
failure - it never decides whether a message has already been sent.
"""

from __future__ import annotations

import smtplib
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Protocol


class NotifyNotConfigured(RuntimeError):
    """Raised when no delivery binary/target is configured."""


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class Notification:
    """What gets sent, independent of how. `reminder_id`/`category` exist
    for delivery tracking and future per-category routing policy (e.g. email
    vs push) - providers that don't need them simply ignore them."""

    text: str
    reminder_id: int | None = None
    category: str | None = None


class NotificationProvider(Protocol):
    """The entire contract the reminder engine and health self-alert depend
    on. Any provider - Hermes, email today, Web Push later - only ever needs
    to implement this one method."""

    def send(self, notification: Notification) -> NotifyResult: ...


def send_to_all_providers(
    providers: list[NotificationProvider],
    notification: Notification,
    *,
    on_attempt: Callable[[str, NotifyResult], None] | None = None,
) -> tuple[bool, list[str]]:
    """Shared fan-out used by ragra/reminders/dispatch.py, ragra/health.py
    and ragra/reminders/class_reminders.py: attempts every configured
    provider (no short-circuit on the first success) so a caller can tell
    whether at least one delivered. Returns (delivered, errors) - delivered
    is True if any provider succeeded; errors collects every failure (empty
    if all succeeded).

    `on_attempt(provider_name, result)` is invoked once per provider, and is
    how per-provider delivery gets recorded without this module ever
    learning about the database. Keeping persistence on the caller's side is
    what lets the notification layer stay a pure boundary. A recorder that
    raises must never lose an already-successful delivery, so its failure is
    contained here.
    """
    errors: list[str] = []
    delivered = False
    for provider in providers:
        result = provider.send(notification)
        if on_attempt is not None:
            try:
                on_attempt(type(provider).__name__, result)
            except Exception:  # noqa: BLE001 - bookkeeping must not undo a real send
                pass
        if result.ok:
            delivered = True
        else:
            errors.append(result.error or "unknown error")
    return delivered, errors


def send_notification(
    *,
    hermes_bin: Path | None,
    target: str | None,
    message: str,
    timeout_seconds: int = 30,
) -> NotifyResult:
    if not hermes_bin or not target:
        return NotifyResult(ok=False, error="notification delivery is not configured")

    try:
        proc = subprocess.run(
            [str(hermes_bin), "send", "--to", target, message, "--quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return NotifyResult(ok=False, error=str(exc))

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        return NotifyResult(ok=False, error=detail)

    return NotifyResult(ok=True)


@dataclass(frozen=True)
class HermesProvider:
    """Optional, advanced-personal-integration provider - for installations
    that already run Hermes, wrapping `hermes send` (see send_notification
    above). Never imported by ragra/reminders/dispatch.py or ragra/health.py
    directly; only ragra/cli.py's _build_providers() constructs one, and
    only when HERMES_BIN and RAGRA_NOTIFY_TARGET are both configured. Ragra
    core works fully without it - an empty provider list is a normal,
    supported state, not an error."""

    hermes_bin: Path
    target: str

    def send(self, notification: Notification) -> NotifyResult:
        return send_notification(hermes_bin=self.hermes_bin, target=self.target, message=notification.text)


def _redact(text: str, *secrets: str | None) -> str:
    """Defense-in-depth: strip any configured secret value out of an error
    string before it can ever reach NotifyResult.error, storage, or logs -
    even though smtplib exceptions don't normally echo back the password.
    See docs/INTERFACES.md contract #1: providers report failure, they never
    get to leak what they were configured with."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


@dataclass(frozen=True)
class EmailProvider:
    """Optional email provider, speaking SMTP directly via the standard
    library - no third-party dependency. Constructed only by
    ragra/cli.py's _build_providers(), only when SMTP host/from/to are all
    configured (see ragra/config.py's RAGRA_SMTP_* / RAGRA_EMAIL_TO). Ragra
    core works fully without it, same as HermesProvider."""

    host: str
    port: int
    from_address: str
    to_address: str
    username: str | None = None
    password: str | None = None
    use_ssl: bool = False
    base_url: str | None = None  # optional deep link to the dashboard, appended to the body

    def send(self, notification: Notification) -> NotifyResult:
        subject = f"Ragra: {notification.category}" if notification.category else "Ragra notification"
        body = notification.text
        if self.base_url:
            body = f"{body}\n\nView in Ragra: {self.base_url}"

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_address
        message["To"] = self.to_address
        message.set_content(body)

        try:
            smtp_class = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
            with smtp_class(self.host, self.port, timeout=30) as smtp:
                if not self.use_ssl:
                    smtp.starttls()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            return NotifyResult(ok=False, error=_redact(str(exc), self.password))

        return NotifyResult(ok=True)
