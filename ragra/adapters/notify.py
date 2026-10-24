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


# Transport-failure classification codes. These name the layer that
# actually failed, which matters because a delivery failure used to reach
# the user as an opaque string and get misread as an authentication
# problem. A notification provider performs no Google operation of any
# kind, so no code here may ever describe one: whatever breaks in
# delivery, it is not OAuth, not a token, and not Google Classroom.
NOT_CONFIGURED = "NOT_CONFIGURED"
HERMES_TRANSPORT_ERROR = "HERMES_TRANSPORT_ERROR"      # binary missing, unlaunchable, or timed out
HERMES_COMMAND_ERROR = "HERMES_COMMAND_ERROR"          # binary ran and rejected the command
EMAIL_TRANSPORT_ERROR = "EMAIL_TRANSPORT_ERROR"        # SMTP refused or unreachable


def _platform_transport_error(target: str | None) -> str:
    """The delivery platform's own failure, named after that platform.

    Hermes targets are "<platform>:<channel>", so a WhatsApp send failure
    reports WHATSAPP_TRANSPORT_ERROR rather than a generic one - the point
    of these codes is to say which layer broke.
    """
    platform = (target or "").split(":", 1)[0].strip().upper()
    return f"{platform}_TRANSPORT_ERROR" if platform.isalnum() and platform else "PLATFORM_TRANSPORT_ERROR"


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    error: str | None = None
    # Which layer failed. Callers persist `error`, which is prefixed with
    # this code, so the classification survives into notification_deliveries
    # without a schema change.
    code: str | None = None


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


def _failure(code: str, detail: str) -> NotifyResult:
    """Build a classified failure.

    The code is prefixed onto `error` because every caller persists
    `error` and nothing persists `code` - this is what carries the
    classification into the delivery record and the tick log without a
    migration. Detail is kept, since the code names the layer but not
    what went wrong inside it.
    """
    detail = detail.strip()
    return NotifyResult(ok=False, error=f"{code}: {detail}" if detail else code, code=code)


def send_notification(
    *,
    hermes_bin: Path | None,
    target: str | None,
    message: str,
    timeout_seconds: int = 30,
) -> NotifyResult:
    if not hermes_bin or not target:
        return _failure(NOT_CONFIGURED, "notification delivery is not configured")

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
        # Never reached the transport at all: the binary is missing, not
        # executable, or hung. Nothing was delivered and nothing was
        # authenticated.
        return _failure(HERMES_TRANSPORT_ERROR, str(exc))

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        # `hermes send` documents: 0 ok, 1 delivery/backend error, 2 usage
        # error. Exit 2 is Ragra calling the CLI wrongly (our bug); exit 1
        # is the messaging platform itself refusing or failing the send.
        code = HERMES_COMMAND_ERROR if proc.returncode == 2 else _platform_transport_error(target)
        return _failure(code, detail)

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
            return _failure(EMAIL_TRANSPORT_ERROR, _redact(str(exc), self.password))

        return NotifyResult(ok=True)


@dataclass(frozen=True)
class BrevoEmailProvider:
    """Email over HTTPS (via `requests`) through Brevo's transactional API.

    Exists because some hosts (Railway's trial and hobby plans among them)
    block outbound SMTP, which makes `EmailProvider` fail with "Network is
    unreachable". HTTPS on 443 is not blocked. `from_address` must be a
    sender verified in Brevo. Selected by RAGRA_BREVO_API_KEY; see
    ragra/notifications/preferences.py."""

    api_key: str
    from_address: str
    to_address: str
    base_url: str | None = None
    endpoint: str = "https://api.brevo.com/v3/smtp/email"

    def send(self, notification: Notification) -> NotifyResult:
        import requests

        subject = f"Ragra: {notification.category}" if notification.category else "Ragra notification"
        body = notification.text
        if self.base_url:
            body = f"{body}\n\nView in Ragra: {self.base_url}"

        try:
            response = requests.post(
                self.endpoint,
                headers={"api-key": self.api_key, "accept": "application/json"},
                json={
                    "sender": {"name": "Ragra", "email": self.from_address},
                    "to": [{"email": self.to_address}],
                    "subject": subject,
                    "textContent": body,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            return _failure(EMAIL_TRANSPORT_ERROR, _redact(str(exc), self.api_key))

        if response.status_code >= 300:
            detail = f"HTTP {response.status_code}: {response.text[:200]}"
            return _failure(EMAIL_TRANSPORT_ERROR, _redact(detail, self.api_key))
        return NotifyResult(ok=True)
