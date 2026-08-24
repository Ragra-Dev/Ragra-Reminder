# Deadline Engine Rules

Maintain two independent concepts:

- actual_deadline: authoritative academic deadline
- personal_deadline: Hashim's intended completion time

A personal deadline may change without changing the academic deadline.

Reminder scheduling must be deterministic and idempotent.

Completed tasks must not receive future reminders.
Cancelled/deleted source tasks must not retain active reminders.
Short-deadline tasks need compressed reminders without notification spam.

Preserve history when deadlines or states change.
