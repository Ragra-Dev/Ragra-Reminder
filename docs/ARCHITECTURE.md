# Ragra Architecture

## Initial shape
Prefer a modular monolith.

Conceptual flow:

Google Classroom -> sync -> database -> task/deadline engine -> scheduler
                                                     |
                                                     +-> Google Calendar
                                                     |
                                                     +-> Hermes -> WhatsApp/Telegram/iOS Messages
                                                     |
                                                     +-> dashboard
                                                     |
                                                     +-> AI planner (optional)

## Hermes
Hermes is an existing local automation system. Reuse its working integrations through a narrow adapter. Do not rewrite Hermes unless there is a demonstrated blocker.

## AI boundary
AI can:
- classify
- estimate workload
- propose schedules
- explain priorities
- answer questions through Ragra tools

AI cannot:
- invent academic deadlines
- silently modify authoritative source data
- bypass deterministic scheduling constraints

## Failure isolation
Classroom, Calendar, Hermes, and AI failures should not corrupt the core Ragra state.

## Single-user optimization
Choose the simplest reliable local/managed database and scheduler after inspecting the existing environment. Do not introduce distributed infrastructure unless a concrete requirement appears.

## Stack decision (confirmed)

**Backend: Python 3.11+, stdlib `sqlite3`, FastAPI + Jinja2 for the dashboard.**

Rationale, from direct inspection of `hermes_cli/classroom/`:

- `oauth.py` and `google_client.py` are clean, read-only, and have zero coupling to
  Hermes' Kanban DB or agent runtime. Ragra imports them directly (via `sys.path`
  insertion, `HERMES_REPO_PATH` env var) rather than reimplementing Google OAuth
  and the Classroom API wrapper. This is a direct-import reuse, not an adapter,
  because the code is already decoupled and stable.
- `tasks.py` and `analysis.py` (AI difficulty estimation, Kanban task creation) are
  **not** reused — they hard-depend on `hermes_cli.kanban_db`, which Ragra must not
  depend on (its own state must survive Hermes being unavailable/changed).
- `registration.py` confirms FAST-NUCES already separates sections into distinct
  Classroom courses (one course per course+section combination). Enrollment/course
  metadata already disambiguates sections — no NLP/LLM parsing is needed for
  section detection.
- Notifications go through `hermes send --to <target> "message"` (`bin/hermes.exe`
  on Windows), a documented one-shot CLI command with no agent loop / no LLM cost.
  This is a genuine adapter (subprocess boundary): Ragra never imports Hermes'
  messaging/gateway internals, so a broken Hermes install can't corrupt Ragra state
  and a Hermes upgrade can't break Ragra's imports.
- SQLite via stdlib `sqlite3` (no ORM) matches Hermes' own `kanban_db.py` style,
  keeps the dependency footprint minimal, and is more than sufficient for a
  single-user database. Revisit only if a concrete multi-writer or remote-access
  requirement appears.
- FastAPI + Jinja2 for the dashboard: smallest reliable way to get a real browser
  UI without a separate frontend build step/toolchain.

Consequence: Ragra depends on Hermes at two narrow points (`hermes_cli.classroom.{oauth,google_client}`
import, and the `hermes send` subprocess). Both are already-stable, already-decoupled
surfaces of Hermes, so this is direct reuse rather than premature abstraction — but
each is still wrapped in a single Ragra module (`ragra/adapters/classroom.py`,
`ragra/adapters/notify.py`) so a future breaking Hermes change only requires editing
one file.
