# Ragra Architecture

## Initial shape
Prefer a modular monolith.

Conceptual flow:

Google Classroom -> sync -> database -> task/deadline engine -> scheduler
                                                     |
                                                     +-> Google Calendar
                                                     |
                                                     +-> notification providers (optional)
                                                     |
                                                     +-> dashboard
                                                     |
                                                     +-> AI planner (optional)

## Core vs. optional layers
Core: Google Classroom sync, FAST timetable sync, Google Calendar sync, and the
deadline/reminder engine. These have no dependency on Hermes or any other
personal tooling and must work with none of it installed.

Notification layer: due reminders are delivered through pluggable providers
(`ragra/adapters/*_notify.py`) behind a common `send_notification()` interface.
A provider going unconfigured or failing never affects Classroom/Calendar/FAST
sync or the reminder engine's own state - reminders simply stay pending until a
provider is available. Current providers: direct Telegram Bot API delivery, and
an optional personal Hermes provider (for Hashim's own installation only,
shelling out to `hermes send`, never importing Hermes internals).

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
Classroom, Calendar, notification providers, and AI failures should not corrupt
the core Ragra state.

## Single-user optimization
Choose the simplest reliable local/managed database and scheduler after inspecting the existing environment. Do not introduce distributed infrastructure unless a concrete requirement appears.

## Stack decision (confirmed)

**Backend: Python 3.11+, stdlib `sqlite3`, FastAPI + Jinja2 for the dashboard.**

- Classroom and Calendar each use Ragra's own Google OAuth client
  (`ragra/adapters/classroom.py`, `ragra/adapters/calendar.py`) with their own
  narrowly-scoped credentials - read-only Classroom scopes, and
  `calendar.events` only for Calendar. No import of any Hermes module.
- FAST-NUCES already separates sections into distinct Classroom courses (one
  course per course+section combination), so no NLP/LLM parsing is needed for
  section detection.
- SQLite via stdlib `sqlite3` (no ORM) keeps the dependency footprint minimal
  and is more than sufficient for a single-user database.
- FastAPI + Jinja2 for the dashboard: smallest reliable way to get a real
  browser UI without a separate frontend build step/toolchain.
