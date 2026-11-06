"""The Telegram bot: linking a chat to an account, and answering questions.

Same shape as ragra/web/whatsapp.py, and the same three separations:

  - authenticity: Telegram echoes back a secret we registered with the webhook
    in the X-Telegram-Bot-Api-Secret-Token header; a request without it is
    refused before its body is read, and with no bot token configured the
    route refuses everything.
  - identity: a chat is only ever tied to an account by the link handshake -
    the person presses "Connect Telegram" while signed in, which mints a
    one-time code that arrives here as "/start <code>". The chat id is never
    accepted from a form.
  - answering: ragra/chat.py, scoped to the one account the chat belongs to.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from ragra import chat
from ragra.adapters.notify import Notification, TelegramProvider
from ragra.config import Config, load_config
from ragra.db import repo
from ragra.db.connection import connect_closing

NOT_LINKED_TEXT = (
    "This Telegram chat isn't linked to a Ragra account yet. Open Ragra, go to "
    "Account > Notifications and press Connect Telegram."
)
EXPIRED_TEXT = "That link has expired or was already used. Open Ragra > Account and press Connect Telegram again."
CONNECTED_TEXT = (
    "Connected! New announcements, assignments and deadlines for your section will "
    "arrive here.\n\n"
)


def webhook_secret(bot_token: str) -> str:
    """The secret registered with Telegram's setWebhook and checked on every
    request. Derived from the bot token, so there is nothing extra to
    configure and it is unguessable without the token itself."""
    return hashlib.sha256(("ragra-telegram|" + bot_token).encode("utf-8")).hexdigest()[:48]


def secret_is_valid(*, header: str | None, bot_token: str | None) -> bool:
    if not bot_token or not header:
        return False
    return hmac.compare_digest(header, webhook_secret(bot_token))


def parse_update(body: bytes) -> tuple[str, str] | None:
    """(chat id, text) for a private text message, else None. Group chats,
    edits, stickers and everything else are ignored."""
    try:
        update = json.loads(body)
    except ValueError:
        return None
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat_info = message.get("chat")
    text = message.get("text")
    if not isinstance(chat_info, dict) or chat_info.get("type") != "private":
        return None
    chat_id = chat_info.get("id")
    if not isinstance(chat_id, int) or not isinstance(text, str) or not text.strip():
        return None
    return str(chat_id), text.strip()


def _command_to_text(text: str) -> str:
    """"/pending@RagraBot" -> "pending", so Telegram's slash commands reach the
    same handler as typed words."""
    if text.startswith("/"):
        return text[1:].split("@", 1)[0].split(None, 1)[0] if len(text) > 1 else ""
    return text


def build_reply(db_path: Path, chat_id: str, text: str, config: Config) -> str:
    with connect_closing(db_path) as conn:
        if text.startswith("/start"):
            code = text[len("/start"):].strip()
            if code:
                user_id = repo.consume_telegram_link_code(conn, code=code)
                if user_id is None:
                    return EXPIRED_TEXT
                repo.connect_telegram_chat(conn, user_id=user_id, chat_id=chat_id)
                return CONNECTED_TEXT + chat.HELP_TEXT
            return chat.HELP_TEXT if repo.user_id_for_telegram(conn, chat_id=chat_id) else NOT_LINKED_TEXT

        user_id = repo.user_id_for_telegram(conn, chat_id=chat_id)
        if user_id is None:
            return NOT_LINKED_TEXT
        return chat.answer(conn, config, user_id=user_id, text=_command_to_text(text))


def reply_to_telegram(
    db_path: Path, chat_id: str, text: str, *, config: Config | None = None, sender=None
) -> str:
    """Work out and send the reply to one inbound message. Never raises: it
    runs after the webhook has answered Telegram."""
    config = config or load_config()
    try:
        reply = build_reply(db_path, chat_id, text, config)
        if sender is None and config.telegram_bot_token:
            sender = TelegramProvider(bot_token=config.telegram_bot_token, chat_id=chat_id)
        if sender is not None:
            sender.send(Notification(text=reply, category="Chat"))
        return reply
    except Exception:  # noqa: BLE001 - nothing to report this to; see docstring
        return ""


def register_webhook(config: Config) -> str:
    """Tell Telegram where to deliver messages. Returns a one-line status.
    Needs RAGRA_WEB_BASE_URL to be the public https address of this app."""
    import requests

    if not config.telegram_bot_token:
        return "telegram: not configured"
    if not config.web_base_url or not config.web_base_url.startswith("https://"):
        return "telegram: RAGRA_WEB_BASE_URL must be the public https address"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/setWebhook",
            json={
                "url": config.web_base_url.rstrip("/") + "/telegram/webhook",
                "secret_token": webhook_secret(config.telegram_bot_token),
                "allowed_updates": ["message"],
            },
            timeout=20,
        )
        ok = response.status_code < 300 and response.json().get("ok")
    except (requests.RequestException, ValueError) as exc:
        return "telegram: webhook registration failed: " + str(exc).replace(config.telegram_bot_token, "***")
    return "telegram: webhook registered" if ok else f"telegram: webhook registration refused (HTTP {response.status_code})"
