"""The inbound side of the WhatsApp channel (Meta Cloud API webhook).

Meta calls this app when someone writes to the Ragra number. Three things are
kept strictly separate here:

  - authenticity: every POST is signed by Meta with the app secret, and an
    unsigned or mis-signed request is refused before its body is even parsed.
    There is no session or CSRF token on this route - the signature is the
    authentication, and with no secret configured the route refuses everything
    rather than answering anyone.
  - identity: the sender's number selects exactly one account
    (repo.user_id_for_whatsapp); a number nobody owns gets a polite pointer to
    the Account page and never any data.
  - answering: ragra/chat.py does that, scoped to the one account found.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from ragra import chat
from ragra.adapters.notify import Notification, WhatsAppCloudProvider
from ragra.config import Config, load_config
from ragra.db import repo
from ragra.db.connection import connect_closing

NOT_LINKED_TEXT = (
    "This WhatsApp number isn't linked to a Ragra account yet. Open Ragra, go to "
    "Account > Notifications, add this number under WhatsApp and tick it."
)


def signature_is_valid(*, body: bytes, header: str | None, secret: str | None) -> bool:
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def extract_text_messages(payload: object) -> list[tuple[str, str]]:
    """(sender number, text) for every text message in a webhook payload.
    Delivery receipts, reactions and media are skipped - Meta sends those to
    the same URL and they are not questions."""
    found: list[tuple[str, str]] = []

    def _items(value: object) -> list[dict]:
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    if not isinstance(payload, dict):
        return found
    for entry in _items(payload.get("entry")):
        for change in _items(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in _items(value.get("messages")):
                if message.get("type") != "text":
                    continue
                body = message.get("text")
                sender = str(message.get("from") or "")
                text = str(body.get("body") or "") if isinstance(body, dict) else ""
                if sender.isdigit() and text.strip():
                    found.append((sender, text))
    return found


def parse_payload(body: bytes) -> object:
    try:
        return json.loads(body)
    except ValueError:
        return None


def reply_to_whatsapp(
    db_path: Path, number: str, text: str, *, config: Config | None = None, sender=None
) -> str:
    """Work out and send the reply to one inbound message. Never raises: this
    runs after the webhook has already answered Meta, so a failure here must
    be contained rather than surface anywhere."""
    config = config or load_config()
    try:
        with connect_closing(db_path) as conn:
            user_id = repo.user_id_for_whatsapp(conn, number=number)
            reply = (
                NOT_LINKED_TEXT
                if user_id is None
                else chat.answer(conn, config, user_id=user_id, text=text)
            )
        if sender is None and config.whatsapp_token and config.whatsapp_phone_number_id:
            sender = WhatsAppCloudProvider(
                token=config.whatsapp_token,
                phone_number_id=config.whatsapp_phone_number_id,
                to_number=number,
            )
        if sender is not None:
            sender.send(Notification(text=reply, category="Chat"))
        return reply
    except Exception:  # noqa: BLE001 - nothing to report this to; see docstring
        return ""
