"""Remote-capable notification adapter: a direct HTTPS call to Telegram's
Bot API, with zero dependency on Hermes, its gateway, or any local
executable. This exists because `ragra/adapters/notify.py` (the Hermes
path) shells out to a Windows binary that reads Hermes' own local session
files - genuinely not portable to a remote worker. Telegram itself needs
none of that: a bot token is a plain, portable credential, and sending a
message is one POST request.

This is a second, narrow implementation of the same intent as
send_notification() - not a replacement for it, and not a rewrite of
anything in ragra/reminders/dispatch.py. Which one dispatch.py calls is a
config/environment choice (see ragra/config.py); the reminder/business
logic never changes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class TelegramNotifyNotConfigured(RuntimeError):
    """Raised when no bot token/chat id is configured."""


@dataclass(frozen=True)
class TelegramNotifyResult:
    ok: bool
    error: str | None = None


def send_telegram_notification(
    *,
    bot_token: str | None,
    chat_id: str | None,
    message: str,
    timeout_seconds: int = 15,
) -> TelegramNotifyResult:
    """Single delivery attempt via Telegram's Bot API. Idempotency is the
    caller's responsibility, exactly like ragra/adapters/notify.py - this
    only ever reports success or failure for one send."""
    if not bot_token or not chat_id:
        return TelegramNotifyResult(ok=False, error="notification delivery is not configured")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("description", str(exc))
        except Exception:
            detail = str(exc)
        return TelegramNotifyResult(ok=False, error=detail)
    except Exception as exc:  # noqa: BLE001 - network/timeout failures must not raise
        return TelegramNotifyResult(ok=False, error=str(exc))

    if not body.get("ok"):
        return TelegramNotifyResult(ok=False, error=body.get("description", "unknown Telegram API error"))

    return TelegramNotifyResult(ok=True)
