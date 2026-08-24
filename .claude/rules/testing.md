# Testing Rules

For changes involving deadlines, synchronization, reminders, calendar events, or timetable events, test idempotency and state transitions.

At minimum verify:
- repeated sync does not duplicate records
- deadline changes update the existing task
- completed tasks stop future reminders
- missed tasks transition correctly
- cancelled/rescheduled classes do not produce stale reminders
- calendar events are updated rather than duplicated
- notification retries do not duplicate sends

Run the smallest relevant test set after each meaningful change, then broader tests before declaring a milestone complete.
