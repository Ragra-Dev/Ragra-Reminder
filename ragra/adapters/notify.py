"""Notification layer: a small, provider-neutral boundary between the
reminder engine and however a message actually gets delivered.

ragra/reminders/dispatch.py and ragra/health.py depend only on the
NotificationProvider protocol below (`send(message) -> NotifyResult`) - they
never import or know about Hermes, WhatsApp, Web Push, or email specifically.
HermesProvider is the one concrete implementation that ships today: an
optional, advanced-personal-integration provider wrapping Hermes' `hermes
send` CLI. This is a pure process-boundary call - Ragra never imports
Hermes' messaging/gateway internals, so a broken or upgraded Hermes install
cannot corrupt Ragra state or break Ragra's imports. Future providers (Web
Push, email) implement the same protocol and get added to
ragra/cli.py's _build_providers() only - the reminder/health code that
calls them never changes, and never needs to know Hermes exists.

Idempotency is the caller's responsibility (see ragra/reminders/dispatch.py):
a provider only performs a single delivery attempt and reports success or
failure - it never decides whether a message has already been sent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class NotifyNotConfigured(RuntimeError):
    """Raised when no delivery binary/target is configured."""


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    error: str | None = None


class NotificationProvider(Protocol):
    """The entire contract the reminder engine and health self-alert depend
    on. Any provider - Hermes today, Web Push/email later - only ever needs
    to implement this one method."""

    def send(self, message: str) -> NotifyResult: ...


def send_to_all_providers(providers: list[NotificationProvider], message: str) -> tuple[bool, list[str]]:
    """Shared fan-out used by both ragra/reminders/dispatch.py and
    ragra/health.py: attempts every configured provider (no short-circuit
    on the first success) so a caller can tell whether at least one
    delivered. Returns (delivered, errors) - delivered is True if any
    provider succeeded; errors collects every failure (empty if all
    succeeded)."""
    errors: list[str] = []
    delivered = False
    for provider in providers:
        result = provider.send(message)
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

    def send(self, message: str) -> NotifyResult:
        return send_notification(hermes_bin=self.hermes_bin, target=self.target, message=message)
