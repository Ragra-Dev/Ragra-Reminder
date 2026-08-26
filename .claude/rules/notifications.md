# Notification Rules

Ragra owns notification intent and never depends on any specific transport.
Delivery goes through the `NotificationProvider` protocol
(`ragra/adapters/notify.py`: `send(message) -> NotifyResult`) - the reminder
engine and health self-alert (`ragra/reminders/dispatch.py`,
`ragra/health.py`) only ever depend on that, never on a named provider.
Idempotency (never resending an already-sent reminder) is the caller's
responsibility, not a provider's.

Current provider: Hermes, an optional, advanced-personal-integration
provider (e.g. for WhatsApp delivery) - never required by Ragra core.
Planned future providers: Web Push and email. Telegram was built and
verified, then deliberately dropped from the product direction (see
`docs/PROJECT_STATUS.md`) - do not reintroduce it without being asked.

Never let notification failure corrupt academic state.
Retry safely.
Never send duplicate notifications for the same event/reminder.
Prefer concise actionable messages.
