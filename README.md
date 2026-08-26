# Ragra

A single-user, local-first academic manager. Pulls real Google Classroom
coursework into a local database, tracks deadlines (separating the
authoritative Classroom deadline from your own intended completion time),
schedules deterministic reminders, delivers them through an existing Hermes
messaging setup, mirrors deadlines onto Google Calendar, and runs
unattended via a Windows scheduled task.

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
anything that talks to Classroom, Calendar, or Hermes. Real credentials and
the personal database live outside this repository (see `.gitignore`) and
are never committed.

## Project layout

- `ragra/` — application code (sync, reminders, Calendar, dashboard, AI
  advisor, CLI)
- `tests/` — the test suite (`pytest`)
- `scripts/install-scheduled-task.ps1` — registers the Windows scheduled
  task that runs Ragra every 15 minutes
- `docs/` — product/domain/architecture context and the project status
  handoff document
- the local development configuration — project rules and secret-scanning hooks used by this repo's
  local development tooling

## Repository conventions and safety tooling

- `the local development guide` / `the local rules directory/` — project operating rules (domain,
  security, testing)
- `the local hooks directory/protect-secrets.py` — a guard that blocks writing
  real secret-shaped material or writing directly into a known credential
  file
- `the local hooks directory/scan-staged-secrets.py` — a staged-diff secret scanner
- the local development configuration — optional local pre-commit
  installer for the scanner above

These hooks are a safety net, not a substitute for `.gitignore` and
least-privilege credential handling.
