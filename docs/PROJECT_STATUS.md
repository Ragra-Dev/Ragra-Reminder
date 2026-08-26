# Ragra — Project Status

This is a development checkpoint/handoff document, written so the project
can be resumed months later without re-deriving context. Numbers below are
marked "last verified" with the date they were actually queried from the
running system - not assumed.

## Current State

Ragra is a single-user, local-first academic manager for the developer (FAST-NUCES
Islamabad). It is currently a working foundation, not a finished product.

What actually works end-to-end today:
- Pulls real Google Classroom courses, coursework, announcements, and
  materials into a local SQLite database, idempotently.
- Distinguishes `actual_deadline` (authoritative, from Classroom) from
  `personal_deadline` (the developer's own intended completion time) throughout.
- A deterministic reminder engine computes a reminder cadence per task,
  persists it, and dispatches through Hermes with bounded retry.
- Syncs Ragra-owned events onto the developer's real Google Calendar, idempotently.
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
- A small FastAPI + Jinja2 dashboard shows overdue/missed/due-today/due-soon
  tasks, lets you mark a task complete or set a personal target, and has a
  per-task detail view.
- A deterministic daily brief (`ragra brief`) and an optional AI priority
  narrative (`ragra plan`, via Hermes' one-shot completion mode) sit on top
  of the deterministic data - the AI never writes back to task/deadline
  state.

## Verified Integrations

**Google Classroom** - reuses `hermes_cli.classroom.{oauth,google_client}`
directly (not reimplemented). 5 read-only scopes requested; after a Google
Cloud Console branding/scope fix partway through development, all 5 are
now actually granted, confirmed via live `courses.courseWork.list` calls
returning real assignment data. Credential refreshes silently - verified
across many non-interactive `tick` runs with no browser prompt.

**Google Calendar** - a separate, Ragra-owned OAuth credential (not shared
with Classroom or with Hermes' broader Workspace integration), scoped to
`calendar.events` only. Silent refresh confirmed. Real events were verified
to exist on Google's side (read back via the API, not just recorded
locally).

**Hermes** - two narrow, process-boundary integration points, no direct
import of Hermes' internals: `hermes send --to <target> "<message>"` for
notification delivery, and `hermes -z "<prompt>"` for the one-shot AI
advisory call. Both go through Hermes' existing CLI, not a rebuilt client.

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

**Vercel OAuth branding site** - a separate site (outside this repository,
at `<PRIVATE_OAUTH_BRANDING_SITE>`) was used earlier to satisfy Google's OAuth
consent-screen branding/domain requirements so the Classroom scope could be
granted. It was not built or modified as part of Ragra's codebase and is
intentionally excluded from this repository.

## Remote Execution (laptop-off problem) - investigated, not yet deployed

The local Windows scheduler cannot run while the laptop is powered off.
The intended fix is **Google Cloud Run Jobs + Cloud Scheduler +
Cloud-Storage-backed SQLite + Secret Manager**, in the same Google Cloud
project already used for Classroom/Calendar/FAST - chosen after verifying
(not assuming) the actual free-tier terms directly against Google's own
current documentation.

**Verified, not assumed:**
- Cloud Run's Always Free tier (2M requests, 180K vCPU-s, 360K GiB-s per
  month) comfortably covers this workload (a ~45s job every 15 minutes),
  but a **Cloud Billing account with a payment method must be linked to
  the project** to use Cloud Run/Scheduler at all, even to stay at $0.
  Budget alerts are notification-only by default - there is no automatic
  hard spending cap without extra, riskier automation (a budget-triggered
  billing-disable, which turns off the *entire* project, not just the
  overage).
- Cloud Scheduler's free tier (3 jobs/billing account/month) easily covers
  the 1 job needed.
- GitHub Actions was evaluated and rejected as the primary scheduler: its
  free-minutes tier for a private repo (2,000 min/month) is smaller than
  what a 15-minute cadence actually needs (~2,880 runs/month), and
  execution has been unreliable for this project in practice.

**What's implemented and verified so far, without needing any cloud
account access:**
- `ragra/adapters/telegram_notify.py` - a direct Telegram Bot API
  notification path with zero dependency on Hermes, its gateway, or any
  local executable (Hermes' own `hermes send` shells out to a Windows
  binary reading local session files, which a remote worker cannot use for
  WhatsApp specifically). This is additive, not a replacement - the
  existing Hermes path is unchanged and still used locally. Confirmed live
  against Telegram's real API: the HTTP mechanics work correctly and
  errors are reported without ever leaking the bot token; the specific
  chat id must still be independently confirmed by the account owner
  (Hermes' own cached value did not resolve directly via a raw Bot API
  call) before a real message will deliver.

**Explicitly not done yet, and why:** actual Cloud Run/Scheduler
deployment needs three things this environment doesn't have: the `gcloud`
CLI (not installed), a working local Docker daemon (Docker Desktop
installed but not running, needed to build/verify the container image
locally first), and billing enabled on the Google Cloud project (a
financial/account action only the account owner can take). Rather than
fake a deployment, this was left as a clearly-scoped next milestone - see
Planned Roadmap.

## Current Verification

Last verified **2026-08-26**, directly from the running system (not
estimated):

- **186/186 tests passing**
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
running Classroom sync, Calendar sync, and reminder dispatch in sequence,
each isolated from the others' failures, invoked by the Windows Scheduled
Task every 15 minutes.

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
- **Windows Task Scheduler**, not a custom daemon - simplest reliable
  option for a single-user Windows machine.
- **Hermes owns message delivery**; Ragra never reimplements a messaging
  client, only shells out to `hermes send`.
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

- **Remote/always-on execution** - investigated and designed, not deployed
  (see "Remote Execution" above). Ragra still stops entirely when the
  laptop is off.
- **Class-aware reminders** - not implemented. The timetable is synced and
  persisted, but nothing yet reasons about "class starting soon" or
  cross-references it against task deadlines.
- **Task detail pages** - implemented, but basic: title, course, both
  deadlines, status, description (if Classroom provided one), a link back
  to the Classroom post (if present), reminder state, and history. No
  attachment previews or a richer materials view beyond the raw
  description text.
- **Source/material links** - present only insofar as `description` and
  `link` were already captured from Classroom during sync; no dedicated
  materials/attachments model.
- **Snooze/cancel actions** - not on the dashboard. `repo.cancel_task()`
  exists at the code level but has no UI entry point; there is no snooze
  concept at all.
- **Notification fallback channels** - not implemented; `notify.py` takes
  exactly one target string, no primary/fallback chain.
- **Automatic morning brief delivery** - not implemented; `ragra brief`
  exists as a CLI command and `/brief` as a web endpoint, but nothing
  schedules or sends it automatically.
- **Course-code matching** - not implemented; `course_code` is always
  `NULL` in the database (Classroom's API has no such field), and Hermes'
  `matching.py`/`registration.py` (which could derive one) was never
  wired in. The dashboard/reminders correctly fall back to full course
  names everywhere, so this is cosmetic, not a bug.
- **Schema migrations** - no general framework. Only one targeted, one-off
  idempotent column-add exists (for the reminder retry columns). Adding
  the next new column will need the same manual treatment.
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

## Planned Roadmap

### P0 (before relying on Ragra daily again)
1. **Deploy the Cloud Run Jobs + Cloud Scheduler remote execution
   path** - the actual laptop-off fix. Concrete blockers to clear first:
   install/authenticate the `gcloud` CLI, start Docker Desktop (needed to
   build/verify the container image locally before deploying), and enable
   billing on the Google Cloud project. Then: package `ragra tick` behind
   a thin Cloud-Storage download/upload wrapper around the existing SQLite
   file (no rewrite of `ragra/db/repo.py`), store the 4 existing
   credentials plus the Telegram bot token in Secret Manager, wire one
   Cloud Scheduler job, and validate real idempotent execution with the
   Windows scheduled task paused (to rule out a dual-writer conflict while
   this is unproven).
2. Confirm the correct Telegram chat id for `RAGRA_TELEGRAM_CHAT_ID`
   (Hermes' own cached value did not resolve via a raw Bot API call) and
   send one real end-to-end test message through the new adapter.

### P1 (core academic-manager features)
1. Class-aware reminders (built on the FAST timetable, now that it syncs)
2. Richer task detail views (attachments, materials list)
3. Snooze/cancel workflow on the dashboard
4. Automatic morning brief delivery (send `ragra brief` on a schedule)
5. Notification fallback channel
6. Course-code matching (wire in Hermes' registration/matching data)
7. A minimal schema migration mechanism, before the next schema change

### P2 (future intelligence/polish)
1. Improved deadline-risk reasoning (structured, not just prose)
2. AI chat / multi-turn planning ("what should I work on now," "move X to
   tomorrow")
3. Semester analytics (completion rate, per-course stats)
4. ICS/Apple Calendar feed
5. Dashboard pagination for the sections that will grow unbounded
6. General UI/product polish

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

Copy `.env.example` to `.env` and fill in the real local paths
(`HERMES_REPO_PATH`, `HERMES_BIN`) before running anything that talks to
Classroom, Calendar, or Hermes. No secrets belong in `.env.example` or in
this repository - real credentials/tokens live outside the project
directory entirely (under `<LOCAL_APPDATA>\hermes` and
`<LOCAL_APPDATA>\ragra`), and were never part of what's committed here.
