# Ragra — Project Status

This is a development checkpoint/handoff document, written so the project
can be resumed months later without re-deriving context. Numbers below are
marked "last verified" with the date they were actually queried from the
running system - not assumed.

**Two-document model:** ROADMAP.md defines the product direction, phase
order, and implementation plan (the authoritative "what we are building and
why"). PROJECT_STATUS.md reports what is actually implemented and verified
right now (the factual snapshot). Both are required context; read ROADMAP.md
first to understand the plan, then PROJECT_STATUS.md to understand current
progress.

## Current State

**Architecture (current phase, Phase 3):** Ragra is a local-first academic
manager initially built for a FAST-NUCES Islamabad student, built on SQLite and
Windows Task Scheduler. As of Phase 3 it is **multi-user in code** while
remaining local in infrastructure: every stored row has an owner, every query
names one, sign-in is a real Google OAuth round trip, and the scheduled tick
processes each account independently. Hosting, Postgres, and remote execution
remain Phase 4+ (see ROADMAP.md).

**Status:** Currently a working foundation, not a finished product.

What actually works end-to-end today:
- Pulls real Google Classroom courses, coursework, announcements, and
  materials into a local SQLite database, idempotently.
- Distinguishes `actual_deadline` (authoritative, from Classroom) from
  `personal_deadline` (the user's own intended completion time) throughout.
- A deterministic reminder engine computes a reminder cadence per task,
  persists it, and dispatches through a notification provider with bounded
  retry.
- Syncs Ragra-owned events onto the user's real Google Calendar, idempotently.
- Syncs the FAST timetable from its public spreadsheet source, matching
  scraped classes against a small enrollment config to distinguish regular
  and repeat courses (independently, including independent theory/lab
  sections for repeats) - never by color, never by hardcoded section
  letters.
- Runs unattended via a 15-minute Windows Task Scheduler task (hidden, no
  console window, allowed on battery, wakes from sleep if due), with
  per-component health tracking, a self-alert if something breaks
  repeatedly, and a structured 48-hour tick-session diagnostic log
  (separate from application data, auto-purged).
- A FastAPI + Jinja2 dashboard organised as Schedule / Announcements /
  Tasks / Deadlines / Missed: today's classes with times and rooms,
  announcement triage, personal task management, overdue/missed/due-today/
  due-soon deadlines, per-task detail, and a notification delivery log.
- Computes and stores a section-relevance decision for every Classroom item
  during sync, fail-open: only an unambiguous match to a *different*
  section can suppress a notification, and no task is ever hidden.
- Personal (manual) tasks the user owns outright, kept strictly separate
  from Classroom-authoritative data by an enforced write boundary.
- Announces classes starting shortly, and notifies when Classroom moves a
  deadline.
- All user-facing dates and times are rendered in campus local time with an
  explicit zone label; "today" means the campus calendar day, not the UTC
  one.
- A deterministic daily brief (`ragra brief`) and an optional AI priority
  narrative (`ragra plan`, via an optional Hermes one-shot completion call)
  sit on top of the deterministic data - the AI never writes back to
  task/deadline state.

## Verified Integrations

**Google Classroom** - Ragra's own OAuth client and read-only Classroom API
wrapper (`ragra/adapters/classroom.py`), no Hermes dependency. 5 read-only
scopes requested; after a Google Cloud Console branding/scope fix partway
through development, all 5 are now actually granted, confirmed via live
`courses.courseWork.list` calls returning real assignment data. Credential
refreshes silently - verified across many non-interactive `tick` runs with
no browser prompt. The OAuth consent screen's production/publishing status
was a concern going into the soak-test phase (a "Testing"-status app would
have its refresh tokens auto-expire after 7 days, silently breaking sync
mid-soak) - manually verified resolved: repeated `sync`/`tick` runs across
this session complete with no re-authentication required and no browser
prompt. Separately, Google's own refresh-grant response doesn't always echo
`classroom.coursework.me.readonly` back in its `scope` field even though
it's genuinely granted and working (investigated directly by forcing a real
refresh); this produced a harmless but recurring library warning on every
token refresh, now suppressed at its exact source (a filter scoped to that
one message only - any other/real scope or auth problem still surfaces).

**Google Calendar** - a separate, Ragra-owned OAuth credential, scoped to
`calendar.events` only. Silent refresh confirmed. Real events were verified
to exist on Google's side (read back via the API, not just recorded
locally).

**Notifications (optional)** - reminders dispatch through whichever
`NotificationProvider`s are configured (`ragra/adapters/notify.py`), behind
one `send(notification: Notification)` interface (`Notification` carries
`text`/`reminder_id`/`category` - see `docs/INTERFACES.md` contract #2);
`ragra/reminders/dispatch.py`/`ragra/health.py` never know which provider
they're talking to. Every configured provider is attempted per send
(deliberate redundancy - one channel breaking doesn't silently take down
delivery). Two concrete providers exist: `HermesProvider` (optional,
personal, `hermes send --to <target> "<message>"`) and `EmailProvider`
(optional, SMTP via the standard library, credentials from `RAGRA_SMTP_*`
environment variables only, never leaked into a `NotifyResult.error`).
Neither is required for Classroom/Calendar/FAST sync or the reminder
engine itself. Email exists in code but is not configured with real SMTP
credentials in the current deployment, so genuine multi-channel
redundancy is a configuration step away rather than missing code. A direct Telegram Bot API provider was built and verified
working, then deliberately removed from the product direction in favor of
Web Push/email as the planned next providers (see Planned Roadmap) - the
interface was already provider-neutral, so nothing else needed to change
when it was dropped.

**WhatsApp** - one real, explicitly-approved test notification was sent
through Hermes (a personal WhatsApp contact target) as a standalone connectivity test,
deliberately outside the reminder pipeline so it changed zero database
rows. Delivery reported successful (`NotifyResult(ok=True)`). No reminder
has been dispatched through the real pipeline yet - nothing has been due
at a moment real sends were enabled.

**FAST timetable** - synced from its public spreadsheet source (a Google
Sheet, not a university API) via the public gviz endpoint for values and
an optional Sheets API key for true tab discovery (a zero-credential
name-guessing fallback exists if no key is configured). Enrollment
(regular vs. repeat, including independent theory/lab sections for
repeats) lives in `ragra/timetable/enrollment.py` as plain configuration,
never inferred from sheet color or section letters. Verified live against
the real spreadsheet: 13 real enrolled classes, idempotent across repeated
syncs, zero duplicates.

**Windows Task Scheduler** - a task named `Ragra Tick` is registered,
running `ragra tick` every 15 minutes plus at logon; hidden (no visible
console window), allowed to run on battery, wakes the machine from sleep
if a run is due, and rejects overlapping instances. This is a **local
fallback only** - it does nothing when the machine is fully powered off,
which is the actual long-term problem (see "Remote Execution" below).

**Vercel OAuth branding site** - a separate site, maintained outside this
repository, was used earlier to satisfy Google's OAuth consent-screen
branding/domain requirements so the Classroom scope could be granted. It was
not built or modified as part of Ragra's codebase and is intentionally
excluded from this repository.

## Remote Execution (laptop-off problem) — Phase 4 milestone

The local Windows scheduler cannot run while the laptop is powered off.
This is a Phase 4 milestone (Hosted Backend: Postgres + Remote Execution),
scheduled after the single-user product (Phase 2) and user isolation (Phase 3)
are complete.

**Recommended architecture (Phase 4):** PostgreSQL managed database + hosted
container (Fly.io or Railway) + in-process scheduler (APScheduler) initially.
Rationale: one deployment artifact, one log stream, predictable costs, no
billing cliff. See ROADMAP.md §4 for full reasoning and alternatives (Cloud Run
retained only if bundling into existing Google Cloud project is chosen).

**Notification layer readiness:** The notification system is already
provider-neutral (`ragra/adapters/notify.py`'s `NotificationProvider` protocol;
`ragra/reminders/dispatch.py`/`ragra/health.py` depend only on
`send(notification: Notification)`). This is a hard requirement for remote
execution: Hermes' `hermes send` shells out to a Windows binary reading local
session files, which a remote worker cannot use. `EmailProvider` (Phase 1) is
implemented and ready for a remote worker to use instead of Hermes; Web Push
(Phase 5) is still planned.

## Current Verification

Last verified **2026-08-29**, directly from the running system (not
estimated); test count and the Phase 3 migration checks reverified
**2026-09-03** at the close of Phase 3 implementation:

- **712/712 tests passing**
- **Phase 3 migrations (0009-0025) verified against a COPY of the real
  database**, never the original: 708 rows preserved with row contents
  byte-identical, 15 user-owned tables with zero unowned and zero orphaned
  rows, zero foreign-key violations, per-user uniqueness admitting a second
  account while still rejecting same-account duplicates, isolation confirmed
  through the real repository functions, and a full deletion cascade that
  left the real owner's data untouched. Re-running the migrator applied
  nothing further.
- **397/397 tests passing** at the close of Phase 2
- **8** Classroom courses synced
- **175** persisted tasks
- **18** Calendar events
- **56** reminder records
- **18** tasks currently in `MISSED` status, shown on the dashboard via a
  capped preview plus a full `/missed` page
- **13** FAST timetable events, verified idempotent across two consecutive
  real syncs (0 new/0 updated on the second run)
- SQLite running in **WAL** mode (confirmed via `PRAGMA journal_mode`)
- Real WhatsApp test notification delivered successfully (see above)
- Windows scheduled task `Ragra Tick` confirmed running every 15 minutes,
  hidden, allowed on battery, with no duplicate/overlapping runs
- Classroom and Calendar OAuth credentials confirmed to persist and refresh
  silently, with no browser interaction, across repeated `tick` runs
- Structured tick-session diagnostics (start/end/duration/per-stage
  result/errors) recorded per tick with an automatic 48-hour retention
  purge, verified to never touch application data

## Architecture

Two independent sync flows feed one local database, which the reminder
engine and dashboard both read from:

```
Google Classroom --> ragra/sync/classroom_sync.py --> SQLite (WAL)
                                                          |
                                                          v
                                          ragra/reminders/engine.py + dispatch.py
                                                          |
                                                          v
                                              Hermes (hermes send) --> WhatsApp
```

```
Ragra tasks (actual_deadline) --> ragra/sync/calendar_sync.py --> Google Calendar
```

`ragra/cli.py`'s `tick` command is the single unattended entrypoint,
running Classroom sync, Calendar sync, reminder dispatch, and FAST timetable
sync in sequence, each isolated from the others' failures, invoked by the
Windows Scheduled Task every 15 minutes.

**Deterministic:** course/task discovery, deadline tracking, the reminder
cadence and its retry/backoff, missed-task transitions, Calendar event
sync, historical-backlog suppression, health/failure tracking. All of this
is plain code with no model calls, and is what the test suite covers.

**AI-assisted (advisory only):** `ragra/ai/advisor.py` builds a
deterministic, factual snapshot of current tasks and asks Hermes' model
for a prioritized narrative. The AI is explicitly instructed not to invent
facts, and structurally cannot write back to any table - it only returns
text for `ragra plan` / `ragra brief --ai` to print.

## Important Design Decisions

- **Single-user, local-first.** SQLite, no server infrastructure, no
  multi-tenancy - deliberately, per the original product brief.
- **Narrow OAuth scopes, kept separate.** Classroom and Calendar use two
  different credentials; Calendar is intentionally *not* using Hermes'
  broader Workspace credential (which would have bundled unrelated
  Gmail-send/Drive-write access) even though it already existed.
- **Persistent OAuth with silent refresh** - both credentials load and
  refresh non-interactively; interactive consent only happens via explicit
  `classroom-auth` / `calendar-auth` commands, run by hand.
- **Idempotent everywhere that matters** - Classroom sync, Calendar sync,
  and reminder scheduling all dedupe by stable external IDs or
  idempotency keys, verified by repeated real syncs producing zero
  duplicates.
- **`actual_deadline` vs `personal_deadline` are strictly separate** and
  sync never overwrites the latter.
- **Historical reminder backlog is suppressed** - a task already past due
  the moment Ragra first discovers it never generates a flood of
  already-elapsed reminders; anchored to when Ragra itself learned about
  the task, not Classroom's original post date.
- **Bounded reminder retry** (3 attempts, 15-minute backoff) rather than
  retry-forever or fail-immediately, with a genuinely terminal `FAILED`
  state once exhausted.
- **SQLite WAL mode** - chosen once the scheduler, dashboard, and CLI began
  routinely touching the database concurrently.
- **General migration framework** (`ragra/db/migrator.py`, Phase 1) - numbered,
  append-only `.sql` files under `ragra/db/migrations/`, tracked in a
  `schema_migrations` table, applied idempotently from `connect()`. Verified
  non-destructive against a copy of the real database (205 tasks): every
  pre-existing table byte-identical before/after, rerun is a true no-op. The
  two legacy targeted column-migration functions in `connection.py` are
  unchanged and still run first - the new framework only governs schema
  changes from this point forward.
- **Windows Task Scheduler**, not a custom daemon - simplest reliable
  option for a single-user Windows machine.
- **Notification delivery is pluggable and optional**; Ragra never hard-
  depends on any one messaging client - `ragra/reminders/dispatch.py` and
  `ragra/health.py` depend only on
  `NotificationProvider.send(notification: Notification)`, never on
  Hermes/WhatsApp/Web Push/email specifically. Hermes (an optional,
  advanced-personal provider) is only ever shelled out to via `hermes send`,
  never imported. Email (`EmailProvider`) speaks SMTP directly via the
  standard library.
- **AI is never the source of truth** for deadlines, task existence, or
  completion/reminder state - those remain deterministic, by design.
- **No invented semester/term classification.** Investigated whether
  Classroom metadata could reliably distinguish "current" from
  "historical" coursework; it cannot (no term field exists in the API, and
  `courseState` doesn't correlate with term boundaries - confirmed
  empirically). Rather than guess with a fragile heuristic or an arbitrary
  date cutoff, the dashboard instead limits the Missed section to the most
  recent items with a link to the full list - a display fix, not a
  data classification.

## Current Limitations

Verified against the actual code, not assumed:

- **Profile and notification settings have both a web UI and CLI commands.**
  `/account` lets a signed-in user edit their academic profile (program,
  batch year, enrollment start term, enrolled courses as a plain-text list),
  their notification destinations (email, Hermes target), see their Google
  connection status, and delete their account - a typed `delete`
  confirmation, not a checkbox, since it cannot be undone. The equivalent
  CLI commands (`ragra notify-set`, `ragra notify-status`,
  `ragra credentials-import`, `ragra account-delete`) still exist for
  scripting and remain the only path for anything scheduled/unattended.
  Both call the same underlying functions (`ragra/relevance/profile.py`,
  `ragra/notifications/preferences.py`, `ragra/accounts.py`), so there is
  one behavior, reachable two ways.
- **The enrolled-courses editor is a plain-text list, not a dynamic
  add-row form.** Each line is `Course Name | Section | REGULAR or REPEAT |
  batch year | aliases`, parsed server-side with the same `EnrolledCourse`
  validation the rest of the system uses. This needs no client-side
  JavaScript and was the smallest reliable way to make the former
  hand-edited `MY_ENROLLMENT` table user-editable; a friendlier per-row
  widget is future frontend work, not a blocked dependency of anything.
- **Connecting Classroom/Calendar access is still a CLI step, deliberately.**
  `/account` shows connection status read-only. Granting or re-granting
  that access has always been an interactive, local consent flow
  (`ragra classroom-auth` / `calendar-auth`) that only proceeds after
  explicit human go-ahead at a terminal; Phase 3 changed where the
  resulting token is stored (encrypted, per account - see
  `ragra/adapters/google_credentials.py`), not how it is granted. Building
  a second, web-triggered OAuth consent screen for this was out of scope.
- **Account deletion is local, and says so.** It removes every row the
  account owned and destroys the stored Google credential, but it does not
  withdraw the grant from the user's Google account - that has to be done at
  myaccount.google.com/permissions. The deletion receipt states this
  explicitly rather than letting the user assume otherwise.
- **One deliberate authentication exception.** A deployment with sign-in
  unconfigured, reached from loopback, holding exactly one never-signed-in
  account continues to work without signing in. All three conditions are
  required, so a second account or a request from the network ends it. This
  exists so introducing identity did not lock the existing owner out of
  their own dashboard; it is narrow, tested, and ends the moment sign-in is
  configured.
- **Credential encryption depends on an environment key.** With
  `RAGRA_CREDENTIAL_KEY` unset, Google credentials cannot be stored or read
  at all - Ragra fails closed rather than falling back to plaintext. Losing
  the key means re-granting every authorization; there is no recovery path,
  by design.

- **Remote/always-on execution** - investigated and designed, not deployed
  (see "Remote Execution" above). Ragra still stops entirely when the
  laptop is off.
- **Class-aware reminders** - implemented in Phase 2. Class occurrences are
  computed on demand from the weekly timetable pattern (never materialised -
  see `ragra/timetable/schedule.py`), and a class starting within the
  lookahead window is announced once through the same provider-neutral
  notification layer. Not yet cross-referenced against task deadlines
  ("you have a lab and an assignment due the same afternoon"), which
  remains future work.
- **Task detail pages** - implemented, but basic: title, course, both
  deadlines, status, description (if Classroom provided one), a link back
  to the Classroom post (if present), reminder state, and history. No
  attachment previews or a richer materials view beyond the raw
  description text.
- **Source/material links** - present only insofar as `description` and
  `link` were already captured from Classroom during sync; no dedicated
  materials/attachments model.
- **Snooze** - still not implemented; there is no snooze concept at all.
  Cancel now has a dashboard entry point, but only for manual tasks:
  cancelling a Classroom-sourced task raises `TaskSourceViolation`, because
  whether such a task exists is Classroom's decision (docs/INTERFACES.md
  contract #5).
- **Notification fallback channels** - multi-provider mechanism is built and
  tested (`ragra/adapters/notify.py`; every configured `NotificationProvider`
  is attempted per send). Two providers now exist in code (Hermes, Email -
  Phase 1), but only Hermes has real credentials configured on the current
  installation, so genuine multi-channel redundancy isn't active yet - that's
  a configuration step, not missing code. Web Push (Phase 5) is still
  unimplemented.
- **Automatic morning brief delivery** - not implemented; `ragra brief`
  exists as a CLI command and `/brief` as a web endpoint, but nothing
  schedules or sends it automatically.
- **Course-code matching** - not implemented; `course_code` is always
  `NULL` in the database (Classroom's API has no such field), and Hermes'
  `matching.py`/`registration.py` (which could derive one) was never
  wired in. The dashboard/reminders correctly fall back to full course
  names everywhere, so this is cosmetic, not a bug.
- **Dashboard pagination** - only the Missed section has a preview/full-page
  split (added this session). Due Soon, Recently Completed, and Scheduled
  Reminders have no limit and will grow unbounded over time.
- **ICS/Apple Calendar** - not implemented.
- **AI chat** - not implemented. Only single-shot `ragra plan` /
  `brief --ai` calls exist; no conversational or multi-turn interface, no
  "move this to tomorrow" style commands.
- **Deadline-risk analysis** - only as unstructured prose inside the AI
  advisor's output; no deterministic risk score or dedicated view.
- **Semester analytics** - not implemented (completion rate, per-course
  stats, etc.).

## Roadmap

**ROADMAP.md is the authoritative product roadmap and development plan.**
**PROJECT_STATUS.md is this factual implementation snapshot.**

The roadmap defines nine phases (P0–P8). Current status:

| Phase | Duration | Milestone | Status |
|-------|----------|-----------|--------|
| **P0** | 1–2 days | Repository Hygiene / Clean Clone | COMPLETED|
| **P1** | 1.5–3 wk | Core Academic Intelligence (M1) | COMPLETED |
| **P2** | 3–6 wk | Complete Single-User Product (M2) | IMPLEMENTED (soak test pending) |
| **P3** | 3–5 wk | Identity + User Isolation (local) | PLANNED |
| **P4** | 3–6 wk | Hosted Backend: Postgres + Remote Execution | PLANNED |
| **P5** | 2–4 wk | Web Push + Notification Preferences | PLANNED |
| **P6** | 3–5 wk | Pilot: 1–3 Real Users | PLANNED |
| **P7** | 4–8 wk | Production Hardening + V1 Launch | PLANNED |
| **P8** | Ongoing | Post-V1 (optional features) | PLANNED |

**Key milestones:**
- End of P1: Deterministic relevance filtering + Email provider
- End of P2: Ragra is genuinely useful for personal daily use
- End of P4: Remote execution; laptop can stay off
- End of P6: 1–3 real users live with Ragra
- End of P7: Public v1 launch (Google-verified, 14 success criteria met)

See ROADMAP.md for detailed phase breakdowns, feature lists, allocation,
estimates, and architectural decisions.

## How To Resume

From `<REPOSITORY_ROOT>`:

```
# Activate the existing virtual environment
.venv\Scripts\activate

# Run the test suite
python -m pytest

# Start the dashboard (http://127.0.0.1:8731/)
python -m ragra.cli serve

# Run a manual Classroom + Calendar sync
python -m ragra.cli sync

# Run one full tick manually (sync + reminders, logs to file)
python -m ragra.cli tick

# Check the Windows scheduled task
Get-ScheduledTaskInfo -TaskName "Ragra Tick"
Get-ScheduledTask -TaskName "Ragra Tick"

# Print the deterministic daily brief (optionally with AI notes)
python -m ragra.cli brief
python -m ragra.cli brief --ai
```

If the virtual environment is missing or stale, recreate it:

```
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\pip install pytest fastapi uvicorn jinja2 httpx python-multipart python-dotenv google-api-python-client google-auth google-auth-oauthlib
```

Copy `.env.example` to `.env` and fill in the real local paths before
running anything that talks to Classroom or Calendar. No secrets belong in
`.env.example` or in this repository - real credentials/tokens live outside
the project directory entirely, in a per-user application-data directory
resolved at runtime from the environment (see `ragra/config.py`), and were
never part of what's committed here.
