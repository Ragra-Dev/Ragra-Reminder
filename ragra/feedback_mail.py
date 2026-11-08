"""Email each piece of Contact-us feedback to the team.

The feedback is always saved first (see repo.add_feedback); this module only
adds the email, and never lets an email problem lose or block the feedback. The
result is written back onto the row (emailed_at / email_note) so the owner's
inbox shows whether it went out.

Recipients are the team members on the Contact us page. Set RAGRA_FEEDBACK_TO
(comma separated) on the server to change who receives it. The email goes out
through the same sender the reminders use: Brevo (RAGRA_BREVO_API_KEY) or SMTP,
with RAGRA_EMAIL_FROM as the sender address.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ragra.adapters.notify import (
    BrevoEmailProvider,
    EmailProvider,
    Notification,
    NotificationProvider,
)
from ragra.config import Config, load_config
from ragra.db import repo
from ragra.db.connection import connect_closing

log = logging.getLogger(__name__)

TEAM_EMAILS = ("aounabbassadhana@gmail.com", "hashimamjad047@gmail.com")
KIND_LABELS = {"suggestion": "Suggestion", "bug": "Problem", "praise": "Something they like", "question": "Question"}
NOT_SET_UP = "Email is not set up on the server, so it was saved but not emailed."


def team_recipients() -> list[str]:
    raw = os.environ.get("RAGRA_FEEDBACK_TO", "")
    chosen = [part.strip() for part in raw.split(",") if "@" in part]
    return chosen or list(TEAM_EMAILS)


def _clean(text: str, limit: int = 80) -> str:
    """One line, no control characters: safe inside an email subject."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text or "").strip()[:limit]


def providers_for(config: Config, recipients: list[str]) -> list[NotificationProvider]:
    """One provider per recipient, using whichever email sender the server has."""
    if not config.email_from:
        return []
    if config.brevo_api_key:
        return [
            BrevoEmailProvider(
                api_key=config.brevo_api_key, from_address=config.email_from,
                to_address=to, base_url=config.web_base_url,
            )
            for to in recipients
        ]
    if config.smtp_host:
        return [
            EmailProvider(
                host=config.smtp_host, port=config.smtp_port, from_address=config.email_from, to_address=to,
                username=config.smtp_username, password=config.smtp_password, use_ssl=config.smtp_use_ssl,
                base_url=config.web_base_url,
            )
            for to in recipients
        ]
    return []


def build_notification(*, kind: str, rating: int | None, message: str, name: str, email: str | None, when: str) -> Notification:
    stars = f"{'★' * rating}{'☆' * (5 - rating)}  ({rating} of 5)" if rating else "not given"
    who = _clean(name, 60) or "Someone"
    lines = [
        "New feedback on Ragra Reminder",
        "",
        f"Type:   {KIND_LABELS.get(kind, kind)}",
        f"Rating: {stars}",
        f"From:   {who}" + (f" <{_clean(email, 120)}>" if email else ""),
        f"Sent:   {when}",
        "",
        "Message:",
        message,
    ]
    return Notification(
        text="\n".join(lines),
        category=f"Feedback ({KIND_LABELS.get(kind, kind).lower()}) from {who}",
        link_path="/contact/inbox",
    )


def send_feedback_email(db_path: Path, user_id: int, feedback_id: int) -> None:
    """Background job: email one saved feedback to the team and record the result.
    Never raises - whatever happens is written onto the feedback row."""
    ok_note: str | None = None
    emailed = False
    try:
        config = load_config()
        recipients = team_recipients()
        with connect_closing(db_path) as conn:
            row = repo.get_feedback(conn, user_id=user_id, feedback_id=feedback_id)
            user = repo.get_user(conn, user_id=user_id)
        if row is None:
            return
        providers = providers_for(config, recipients)
        if not providers:
            ok_note = NOT_SET_UP
        else:
            name = (user["display_name"] if user else None) or (user["email"] if user else None) or "Someone"
            notification = build_notification(
                kind=row["kind"], rating=row["rating"], message=row["message"], name=name,
                email=(user["email"] if user else None), when=row["created_at"],
            )
            results = [provider.send(notification) for provider in providers]
            sent = sum(1 for r in results if r.ok)
            emailed = sent > 0
            if sent == len(results):
                ok_note = f"Sent to {sent} of {len(results)} team members."
            else:
                first = next((r.error for r in results if not r.ok), "unknown error")
                ok_note = f"Sent to {sent} of {len(results)}. Problem: {_clean(first or 'unknown error', 160)}"
    except Exception as exc:  # noqa: BLE001 - the feedback is already saved; never let mailing break anything
        log.exception("feedback email failed")
        ok_note = f"Could not email it: {_clean(str(exc), 160)}"
    try:
        with connect_closing(db_path) as conn:
            repo.mark_feedback_emailed(conn, user_id=user_id, feedback_id=feedback_id, emailed=emailed, note=ok_note)
    except Exception:  # noqa: BLE001
        log.exception("could not record the feedback email result")
