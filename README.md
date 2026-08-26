# Ragra

A single-user, local-first academic manager. Pulls real Google Classroom
coursework into a local database, tracks deadlines (separating the
authoritative Classroom deadline from your own intended completion time),
schedules deterministic reminders, mirrors deadlines onto Google Calendar,
and runs unattended via a Windows scheduled task.

Ragra core works independently of any notification provider - reminders
simply stay pending until one is configured. Delivery goes through a
pluggable `NotificationProvider` interface (`send(message)`); Hermes is one
optional, advanced-personal-integration provider for users who already run
it, never a requirement.

See **[docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)** for the full,
up-to-date state of the project: what's actually working, what's
verified, what's missing, and how to resume development.

## Quick start

```
.venv\Scripts\activate
python -m pytest              # run the test suite
python -m ragra.cli serve     # dashboard at http://127.0.0.1:8731/
python -m ragra.cli sync      # one manual Classroom + Calendar sync
python -m ragra.cli tick      # one manual sync + reminder dispatch cycle
```

Copy `.env.example` to `.env` and fill in your local paths before running
anything that talks to Classroom or Calendar. Real credentials and the
personal database live outside this repository (see `.gitignore`) and are
never committed.

## Project layout

- `ragra/` — application code (sync, reminders, Calendar, dashboard, AI
  advisor, CLI)
- `tests/` — the test suite (`pytest`)
- `scripts/install-scheduled-task.ps1` — registers the Windows scheduled
  task that runs Ragra every 15 minutes
- `docs/` — product/domain/architecture context and the project status
  handoff document

## Testing

```
python -m pytest
```

Changes to sync, reminders, calendar events, or timetable events should
verify idempotency (no duplicate records/events on repeated runs) and
correct state transitions (completed tasks stop future reminders,
cancelled/rescheduled classes don't leave stale reminders).

## Status

See [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) for current verified
state, known limitations, and the roadmap.
