"""Notification adapter: Ragra owns notification intent, Hermes owns
delivery. This is a pure process-boundary adapter - Ragra never imports
Hermes' messaging/gateway internals, so a broken or upgraded Hermes install
cannot corrupt Ragra state or break Ragra's imports.

Idempotency is the caller's responsibility (see ragra/reminders/dispatch.py):
this module only performs a single delivery attempt and reports success or
failure - it never decides whether a message has already been sent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class NotifyNotConfigured(RuntimeError):
    """Raised when no delivery binary/target is configured."""


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    error: str | None = None


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
