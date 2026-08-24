# Notification Rules

Ragra owns notification intent; Hermes owns the existing messaging transport where practical.

Use an adapter boundary such as:
send_notification(channel, message, idempotency_key)

Supported personal channels may include WhatsApp, Telegram, iOS Messages, web push, and email.

Never let notification failure corrupt academic state.
Retry safely.
Never send duplicate notifications for the same event/reminder.
Prefer concise actionable messages.
