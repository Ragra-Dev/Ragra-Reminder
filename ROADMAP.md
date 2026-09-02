# RAGRA — PRODUCT ROADMAP

**TOTAL NUMBER OF MAJOR PHASES: 9** (Phase 0 through Phase 8)


This roadmap is the source of truth for product direction, architecture, phase order, dependencies, and definition of done for each phase.

---

## 0. VERIFICATION PASS (done before anything below was written)

| Check | Method | Result |
|---|---|---|
| Test count | `python -m pytest -q` | **211 passed, 0 failed, 0 skipped** — same as the previous review. The archive is byte-identical to the one reviewed; nothing has changed since. |
| Clean-clone installability | Read `pyproject.toml`, ran install | **Still broken.** `[dev]` declares only `pytest`. `httpx` and `python-multipart` are required for 3 web test modules and are absent → collection errors on a fresh clone. |
| Git state | `ls -a` | **No `.git/` in the archive.** Branch/history advice below is generic, not derived from your real history. Re-check `git log`/`git branch` yourself. |
| Docs vs code | Read all of `docs/`, then the code | Three stale claims found (below). |
| Identity model | `grep -rn "user_id\|users"` across `ragra/` and `schema.sql` | **Zero occurrences.** No users table, no tenant key anywhere. |
| Web Push / Email | grep, file listing | **Neither exists.** `HermesProvider` is the only concrete provider. |
| Web OAuth | `adapters/classroom.py:216` | `InstalledAppFlow` + local token file. Installed-app flow only. |
| Remote execution | `scripts/`, `cli.py` | Windows Task Scheduler only. |

### Stale documentation, corrected

1. `docs/PROJECT_STATUS.md` says 186 tests. Actual: **211**.
2. `cli.py:472` help text says timetable sync is *"manual only, not yet in `tick`"*. **Wrong** — `cmd_tick` (`cli.py:336–341`) runs the timetable stage alongside classroom, calendar, reminders.
3. `README.md` describe Ragra as "single-user, local-first" while §1 of your brief describes a hosted multi-user platform. Both are currently true statements about different points in time; the docs need a stated target, not just a stated present.

### New findings this pass (not in the previous review)

4. **The FAST timetable has no consumer.** `timetable_events` is synced, deduped, and idempotent — and then nothing reads it. `repo.list_timetable_events()` is not called by the dashboard, the brief, the reminder engine, or calendar sync. "Today's classes / rooms" — a headline item in your §1 and §10 vision — has **zero** implementation despite the data already being in the database every 15 minutes.
5. **`calendar_events.kind = 'CLASS'` and `calendar_events.timetable_event_id` exist in the schema and are never written.** `calendar_sync.py` only ever writes `ACTUAL_DEADLINE`. Dead schema, which will mislead whoever builds class-aware reminders.
6. Points 4 and 5 together mean the cheapest high-value feature in the whole roadmap — showing a student their day's classes — is currently a few hours of work on data you already have. It is scheduled in Phase 2 accordingly.

---

## 1. WHAT WE ARE BUILDING, AND WHY

**One sentence:** Ragra is a server that watches a FAST-NUCES student's Google
Classroom and timetable, decides what is actually theirs, and makes sure they
find out about it in time — through a web app they can open and through
notifications that arrive whether or not they open it.

**Why it should exist:** the failure mode Ragra targets is not "I forgot my
deadline." It is "the deadline was posted in a shared course, buried under
another section's lab, at 11pm, and I never saw it." That is an information-
filtering problem, not a to-do-list problem. Every feature below is judged
against whether it reduces manual academic tracking (your `docs/PRODUCT.md`
rule). Features that don't, get cut.

**Honest assessment of where the value actually is.** The Classroom sync,
deadline tracking, and reminder engine are table stakes — a dozen apps do
them. The two things that make Ragra defensible for FAST students are
(a) relevance filtering of shared courses and (b) the FAST timetable
integration, which no general product will ever build. Prioritise those. The
generic to-do-app parts are the parts you should build least ambitiously.

**Honest assessment of the risk in that.** Both differentiators are tied to
FAST-specific conventions — a spreadsheet you don't control and free-text
section labels teachers type by hand. Both can break in a week without notice.
That is not a reason to avoid them; it is a reason to build them fail-open
(you already decided this) and to treat "the sheet changed shape" as a
first-class operational event, not an exception.

---

## 2. PHASE MAP

```
P0  Repo hygiene / clean clone            ← 1 day        [nearly done]
P1  Core academic intelligence (M1)       ← 1.5–3 wk     ← YOU ARE HERE
P2  Complete the single-user product (M2) ← 3–6 wk       ← Ragra becomes genuinely useful (to you)
P3  Identity + user isolation (local)     ← 3–5 wk
P4  Hosted backend: Postgres + deploy     ← 3–6 wk       ← Ragra becomes a real hosted product
P5  Web Push + notification preferences   ← 2–4 wk
P6  Pilot: 1–3 real users                 ← 3–5 wk       ← FIRST REAL EXTERNAL USERS
P7  Production hardening + Google review  ← 4–8 wk       ← V1 PUBLIC LAUNCH
P8  Post-v1                               ← ongoing      ← NOT part of v1
```

Two things worth internalising from that map:

- **Ragra is useful to *you* at the end of P2 and to *others* at the end of P6.**
  Everything between P2 and P6 is infrastructure that adds zero features. Four
  phases of plumbing is the honest price of "other students can use it."
- **P7 contains a wait you do not control.** Google OAuth verification for
  sensitive Classroom scopes is a review process measured in weeks. See §12.

---

## 3. RESOLUTIONS TO THE HARD ARCHITECTURAL QUESTIONS (§26)

Every one of these is decided. No open forks.

**Q1 — Should `user_id` be introduced before PostgreSQL?**
**Yes, but not now — as the first task of Phase 3, immediately before the
Postgres migration.** This revises the "do it now as a cheap hedge"
recommendation from my previous review, and I should be clear about why I
changed it. Adding a tenant key *now* means one person rewriting queries in
`repo.py` while the other two are actively appending functions to it during
M1/M2 — maximum merge conflict at the worst moment. Adding it *after* Postgres
means two full-table rewrites instead of one. The narrow window between "M2
code is stable" and "Postgres migration begins" is where it costs least: one
person, one branch, one PR, no parallel churn. The extra queries M1/M2 add in
the meantime are roughly ten, which is a trivial premium over the ~50 already
there.

**Q2 — When exactly should SQLite be replaced?**
When there are two concurrent writers, not when there are two users. Today
`tick` is the only writer. The moment a hosted web app can accept a POST while
a scheduled worker is mid-sync, SQLite-in-object-storage becomes a
last-writer-wins data-loss machine. That happens in Phase 4, so Postgres lands
at the start of Phase 4 — never earlier, never later.

**Q3 — When exactly should remote execution be introduced?**
Phase 4, in the same phase as Postgres, because they are the same problem. A
remote worker needs a database it can reach; a hosted web app needs the same.
Do not deploy the worker against Cloud-Storage-backed SQLite as an interim
step — that interim is a rewrite you'd throw away.

**Q4 — When should Google web login be introduced?**
Phase 3, developed and tested entirely against `http://localhost` (Google
permits localhost redirect URIs for Web application clients). You do not need
hosting to build login. Building it before deploying means Phase 4 deploys a
finished auth system instead of debugging OAuth and Docker simultaneously.

**Q5 — When should Web Push be implemented?**
Phase 5, after HTTPS hosting (P4) and identity (P3) both exist. Push
subscriptions are per-user rows and the service worker requires a secure
origin. Building it earlier means building storage you'd rewrite.

**Q6 — When should Email be implemented?**
Now, in Phase 1 / M1. It is the only notification channel that is fully
testable with zero infrastructure, zero credentials, and zero browser. It also
ends the current single-channel fragility immediately.

**Q7 — Push or email as primary default?**
**Email is the guaranteed floor; Web Push is the preferred surface.** Push has
better UX and worse reliability — iOS Safari only delivers to installed PWAs,
subscriptions expire silently, and browsers drop them without telling you.
Concrete policy: every reminder attempts push if a live subscription exists;
`FINAL_1H` and `DUE_TODAY` additionally always send email. The multi-provider
fan-out in `dispatch.py` already implements exactly this semantics
("delivered if at least one succeeded") — you need per-category routing on top,
not a new mechanism.

**Q8 — Should FastAPI/Jinja evolve into a separate frontend?**
**Keep FastAPI + Jinja through v1. Add HTMX for interactivity. Do not add
React/Next.** The app is read-dominant, server-rendered, has no complex client
state, no offline mode, and no realtime requirement. A second build toolchain
would roughly double the maintenance surface area, for
zero user-visible benefit. You will write exactly one piece of hand-written
JavaScript for v1: the service worker + push subscription handshake (~80
lines, Phase 5). Revisit only if you later want genuine offline support or an
app-like SPA — neither is in v1.

**Q9 — How should official and personal tasks coexist?**
One `tasks` table, discriminated by `source_type`, **not** separate tables.
They share the reminder engine, the calendar sync, the dashboard queries, and
the completion model — splitting tables would fork all four. The boundary is
enforced by a write-guard in `repo.py` that raises if a personal-edit API is
handed a Classroom-sourced task, with a test. That guard *is* your §3 rule in
executable form, and it's stronger than a schema split because it can't be
bypassed by a join.

**Q10 — How should relevance configuration become user-specific?**
`load_profile(user_id) -> UserAcademicProfile`. One function, same signature
forever. Phase 1: reads local config, `user_id` ignored. Phase 3: reads a
`user_profiles` row. No consumer ever imports a module-level constant again.

**Q11 — How should OAuth credentials be stored per user?**
Refresh tokens encrypted at rest in an application table
(`user_google_credentials`), AES-GCM or Fernet, key from the environment /
secret manager and never in the database. Access tokens stay in memory only,
never persisted. Rejected alternative: one cloud-Secret-Manager secret per user
— it costs per secret, has per-project limits, and makes account deletion a
distributed operation.

**Q12 — What belongs on the server vs. the browser?**
Everything on the server. The browser receives HTML, a service worker, and a
push subscription endpoint. No Google tokens, no Google API calls, and no
academic data fetching from client JavaScript, ever.

**Q13 — How should notification failure recovery work?**
Keep the existing bounded retry (3 × 15min → terminal `FAILED`) unchanged. Add
a `notification_deliveries` table recording per-provider outcome per reminder,
so the web app can show delivery state (your §7 requirement) and so a failing
channel is diagnosable. Push `404`/`410` → delete that subscription row
immediately (this is the required behaviour, not an optimisation). Email hard
bounce → mark the address unverified and fall back to push + in-app only.

**Q14 — How should database backups work?**
Phase 4 onward: managed Postgres automated daily backups (every candidate
provider includes this free), plus a weekly `pg_dump` to object storage with a
30-day retention. **Perform one real restore into a scratch database before
Phase 6** — an untested backup is not a backup. Before Phase 4, backup = copy
the SQLite file, which you should automate in Phase 0 (it takes ten minutes and
your real DB has 175 tasks in it).

**Q15 — Minimum infrastructure for 1–3 users?**
One managed Postgres (free tier is sufficient), one container running the web
app, one scheduled job runner, one domain with TLS, environment-variable
secrets, and one error-log destination. That is the entire list.

**Q16 — What is unnecessary until public scale?**
Kubernetes, microservices, Redis, message queues, a CDN, autoscaling, an
observability stack, feature flags, billing, multi-region, and a staging
environment. None of these belong before Phase 7, and most never belong.

**Q17 — What must NEVER be carried into the public product?**
Windows Task Scheduler; `InstalledAppFlow` with a token file on local disk;
`MY_ENROLLMENT` as a module constant; per-machine `.env` as the only config
mechanism; the no-auth "localhost only" dashboard; SQLite in object storage;
any assumption that Hermes exists.

**Q18 — What should be preserved unchanged?**
The reminder engine's purity (`reminders/engine.py` has zero I/O — protect
that). The `NotificationProvider` protocol, fan-out, and bounded retry. The
idempotency discipline: stable external IDs, never titles. The
`actual_deadline` / `personal_deadline` separation. The AI isolation boundary
*and its enforcing test*. The Classroom course-state allow-list. The purity of
`timetable/normalize.py` and `match.py`. The health tracking and tick-session
diagnostics. This is the genuinely good engineering in the repo and none of it
needs to change to become multi-user.

---

## 4. PHASE 0 — REPOSITORY HYGIENE / CLEAN CLONE

**Purpose.** the assigned owner must be able to clone, install, and get a green
suite without asking you anything. Right now they cannot.

**Goal.** `git clone && pip install -e ".[dev]" && pytest` → 211 passing, on a
machine that has never seen this project.

**Features.** None. This is pure enablement.

**Technical work.** Add `httpx` and `python-multipart` to `[project.optional-dependencies].dev`. Fix the stale `cli.py:472` help string. Correct the test count and the timetable-in-tick claim in `docs/PROJECT_STATUS.md`. Move personal specifics into the local override `docs/ragra-local-import.example.md` already describes. Broaden `.gitignore` to `.env*`. Write a `CONTRIBUTING.md` with the setup steps. Add a one-line SQLite backup script.

**Database work.** None.
**Frontend work.** None.
**Backend work.** None.
**Testing work.** Verify the clean-clone path in a fresh virtualenv — actually do it, don't reason about it.

**Security work.** Rotate `RAGRA_SHEETS_API_KEY` — the `.env` with live values left your machine in the archive upload. Set up a `pre-commit` secret-scanning hook every contributor installs. Confirm local-only files are ignored and never zipped into a share again — use `git archive`, not a folder zip.

**Deployment work.** None.
**Dependencies.** None. Start immediately.

**Definition of done.** Fresh clone in a fresh venv installs and passes 211 tests with no manual `pip install`. `CONTRIBUTING.md` exists. Sheets key rotated. No stale claim remains in `docs/`.

**Duration.** 3–6 hours hands-on, 1–2 days calendar.
**Skills to learn.** None.
**Risks.** Only that it gets skipped — a broken clean-clone path is a demoralising first experience for anyone new to the codebase.
**Exit criteria.** A green suite from a fresh clone on a machine that has never seen the project, with no manual setup steps.

---

## 5. PHASE 1 — CORE ACADEMIC INTELLIGENCE (M1) ← CURRENT PHASE

**Purpose.** Build the one piece of logic that is genuinely Ragra's, and give
the notification layer a second real channel.

**Goal.** Ragra can decide, deterministically and fail-open, whether a piece of
Classroom content belongs to this student — and can email them.

**Features.** No user-visible features. This phase is deliberately invisible;
it is the foundation Phase 2 spends entirely.

**Technical work.**
- New package `ragra/relevance/`: `sections.py` (extract section tokens from free text), `engine.py` (`is_relevant()`), `profile.py` (`UserAcademicProfile` + `load_profile`).
- Import `normalize_section` from `timetable/normalize.py`. Do not reimplement it — that is your §11 "do not duplicate" rule's first real test.
- Replace `MY_ENROLLMENT` / `TARGET_PROGRAM` module constants with profile fields (`sync/timetable_sync.py:73, 129, 141`).
- Migration framework: `schema_migrations` table, numbered append-only `.sql` files, applied from `connect()`.
- `Notification` value object replacing `send(message: str)`; `EmailProvider`.

**The corpus decision you have not made.** Your §6 lists the labels that must
match. It does not address the labels that must *not*. Before writing tests,
decide the behaviour for: `Section 3 of the textbook` (chapter reference, not a
section), `CS-101` (course code shaped like a section), `Sections A-D` (a
range — expand it or not?), content with no section token at all, and a title
and description that disagree. **Recommendation: return `UNKNOWN` for all five,
which fails open and notifies.** Expand nothing, resolve nothing. Ranges are
the only one worth revisiting once you have a semester of real data.

**Database work.** Migration framework + baseline. **No relevance columns
yet** — Phase 1's relevance engine stores nothing, which is what lets it merge
independently of the migration work.

**Frontend work.** None.
**Backend work.** `Notification` refactor across `adapters/notify.py`, `reminders/dispatch.py`, `health.py`, `cli.py`. `EmailProvider` construction in `_build_providers()` only.

**Testing work.** Relevance: every label in §6 → `RELEVANT`; `Lab 02 Section E` with profile `CS-C` → `OTHER_SECTION`; all five ambiguous cases → `UNKNOWN`. One property test: **no input ever yields `notify=False` except `OTHER_SECTION`.** Migration: applied to a copy of the real 175-task DB, every row byte-identical, idempotent on rerun. Email: stub SMTP, asserts subject/body/deep-link. Fan-out: two providers, one failing → still `SENT`.

**Security work.** SMTP credentials from environment only, never in a `NotifyResult.error` string. Hold email to the same redaction bar the removed Telegram provider met.

**Deployment work.** None.
**Dependencies.** Phase 0. Interface freeze (§7) before either developer starts.

**Definition of done.** `grep -r import ragra/relevance/` shows no sqlite3, no network, no AI. `TARGET_PROGRAM` no longer referenced at any `timetable_sync.py` call site. All pre-existing timetable tests pass **unmodified**. Migration verified non-destructive against real data. Email provider sends via stub. Suite ≥ 230 tests, green.

**Duration.** 15–25 hours hands-on, 1.5–3 weeks calendar.

**Skills to learn.** what "fail open" means as an
invariant, and why a property test is stronger than example tests here. SQL migration patterns, Python `Protocol` typing.
SMTP mechanics, stub-server test setup.

**Risks.** Relevance scope creep — the temptation to handle every string FAST
has ever produced. Cap it: seven labels from §6, five `UNKNOWN` cases, ship.
Second risk: this phase deliberately avoids touching `repo.py`, keeping it
free for the migration work.

**Exit criteria.** Two independently merged PRs, suite green, and a written
statement of what `is_relevant` will and will not decide.

---

## 6. PHASE 2 — COMPLETE THE SINGLE-USER PRODUCT (M2)

**Purpose.** Turn a working pipeline into a product *you* would be annoyed to
lose. This is the phase where Ragra stops being infrastructure.

**Goal.** Everything in your §1 web-app vision works — for one user, locally.
Today's classes, rooms, personal tasks, announcements, deadline changes,
class-aware reminders, delivery status.

**Features.**
1. Relevance decisions computed during sync and stored; dashboard groups other-section content separately instead of hiding it.
2. Personal tasks: create, edit, reschedule, complete, cancel.
3. Announcement workflow: open → create personal task → ignore/archive. Fully deterministic, no AI.
4. **Timetable surfaced** — today's classes, times, rooms, on the dashboard and in the brief. *(Finding #4: the data is already there and unread.)*
5. **Class-aware reminders** — "DLD starts in 30 min, C-311."
6. Deadline-change notifications — detection already exists; the *notification* does not.
7. Delivery status visible in the web app (`notification_deliveries`).
8. Dashboard restructure: Today / Schedule / Deadlines / Tasks / Announcements.

**Class-aware reminders — where they belong and what they depend on.** They
belong here, not earlier, and they are architecturally separate from relevance.
Dependencies: (a) `timetable_events` — exists and is idempotent; (b) a
recurring-event → concrete-occurrence expansion, which **does not exist**
(rows are weekly patterns: `day_of_week` + `start_time`, not datetimes);
(c) a `tick` cadence fine enough to fire a 30-minute warning — your 15-minute
tick gives ±15 min accuracy, which is acceptable for "class soon" and not for
"class in exactly 30 minutes", so define the reminder as a window, not an
instant; (d) timezone handling — timetable times are local Pakistan wall-clock
while everything else in the DB is UTC ISO, and **this mismatch is a real bug
waiting to happen**; decide it explicitly. Also fill in the dead
`calendar_events.kind='CLASS'` path (finding #5) or delete the columns — don't
leave schema that lies.

**Technical work.** Relevance persistence + sync wiring. Personal-task repo API + write-guard. Announcement routes. Timetable read paths (dashboard, brief). A class-occurrence expander (pure function, testable — model it on `reminders/engine.py`). Deadline-change → notification. Delivery recording.

**Database work.** Migrations for: `tasks.relevance/relevance_reason/relevance_computed_at`; `tasks.parent_task_id` (announcement → personal task link); `notification_deliveries`. All append-only numbered files, so no textual conflicts.

**Frontend work.** Real Jinja templates, HTMX for complete/snooze/create without full page reloads. This is where the dashboard becomes a UI rather than a debug view. Keep it plain — it is going to be rewritten visually at least once, so don't over-invest in styling yet.

**Backend work.** All of the above; `web/app.py` grows substantially and becomes the second merge hotspot after `repo.py`.

**Testing work.** Relevance persistence is non-destructive (uncertain content still visible and still notified). Personal-edit API on a Classroom task **raises**. Announcement → task creates exactly one linked task, idempotently. Class-occurrence expansion across a DST-free but timezone-offset boundary. Deadline change → old reminders cancelled, new scheduled, exactly one change notification. Delivery rows recorded per provider per reminder.

**Security work.** Personal-task routes must not allow editing Classroom fields via form parameters — test it adversarially, since there is still no auth layer to fall back on.

**Deployment work.** None.
**Dependencies.** Phase 1 complete.

**Definition of done.** You personally stop checking Classroom manually. That
is the real test, and it is measurable: use Ragra as your only academic
tracker for two full weeks and count the times you opened Classroom anyway.

**Duration.** 40–70 hours hands-on, **3–6 weeks calendar**. This is the largest
feature phase in the roadmap.

**Merge hotspots this phase:** `repo.py`, `web/app.py`, `classroom_sync.py`.
Rule: append-only within sections, never reorder, and never leave two PRs
touching the same one of these three open for more than 48 hours. The dashboard
restructure is deliberately last and single-owner.

**Skills to learn.** timezone handling — this is
the single most common source of silent bugs in scheduling software, and you
cannot delegate an invariant you don't understand. Also HTTP form handling and
CSRF basics, because Phase 3 puts this on the internet. 
HTMX, Jinja patterns, SQL joins for the dashboard queries. template markup, CRUD boilerplate.

**Risks.** (1) Scope explosion — this phase can absorb infinite polish; cap it
by the two-week dogfood test, not by a feature list. (2) `web/app.py` becoming
a 900-line module; split it into routers when it passes ~300 lines. (3) The
timetable timezone mismatch shipping silently.

**Exit criteria.** Two weeks of real personal use with no manual Classroom
checks. All of §25's outcomes that don't require identity or hosting are met.

---

## 7. PHASE 3 — IDENTITY AND USER ISOLATION (LOCAL)

**Purpose.** Make the code multi-user *before* making the infrastructure
multi-user. Doing these together is how people ship data leaks.

**Goal.** Two accounts can exist on your laptop, sign in with Google, and be
structurally unable to see each other's data — with SQLite, on localhost.

**Features.** Google Sign-In. Sessions. Per-user Classroom/Calendar
authorization. Per-user academic profile (sections, enrollment) editable in the
UI. Per-user notification preferences. Account deletion.

**Technical work.**
- `users` table + `user_id` on `courses`, `tasks`, `timetable_events`, `reminders`, `calendar_events`, and every preference table. **Task 1 of this phase, one person, one PR, nothing else in flight.**
- Migrate `InstalledAppFlow` → web redirect flow (`google-auth-oauthlib` `Flow`, not `InstalledAppFlow`), redirect URI `http://localhost:8731/oauth/callback`. Google permits localhost for Web clients, so all of this is buildable and testable before any hosting exists.
- Encrypted per-user refresh-token storage (Q11).
- Session cookies: signed, `HttpOnly`, `SameSite=Lax`, `Secure` in production.
- `load_profile(user_id)` now reads a DB row — same signature as Phase 1.
- `tick` iterates users instead of assuming one.

**Database work.** The tenant-key migration is the largest single migration in
the roadmap. It must be verified against a copy of your real database.
Plus `sessions`, `user_google_credentials`, `user_profiles`,
`user_notification_preferences`.

**Frontend work.** Login page, account settings, profile editor (sections and
enrollment — the thing that was `MY_ENROLLMENT`), notification preferences,
delete-account flow.

**Backend work.** Auth middleware; a `current_user` dependency; **every**
repo query gains `WHERE user_id = ?`.

**Testing work.** The critical test class: user A's session cannot read, edit,
complete, or receive notifications for any of user B's rows — asserted at the
route level, not the repo level, for tasks, reminders, announcements, timetable
events, and calendar events. Plus: OAuth callback with a bad `state` is
rejected; expired session rejected; account deletion removes every row and
revokes the Google grant; a user with no credentials doesn't break `tick` for
others.

**Security work.** This is the phase where security stops being hygiene and
becomes the product's obligation. CSRF tokens on every mutating form. OAuth
`state` parameter validated. Refresh tokens encrypted. No secrets in logs.
Session fixation prevention. **Budget a dedicated, deep review pass for isolation
alone** — do not merge this phase on a normal PR review.

**Deployment work.** None yet, deliberately.
**Dependencies.** Phase 2 complete and stable. Do not start this with Phase 2 features half-merged.

**Definition of done.** Two accounts on one laptop, complete mutual isolation
proven by tests, account deletion works, and your own account still works after
migrating from the pre-`user_id` schema.

**Duration.** 35–60 hours hands-on, 3–5 weeks calendar.

**Skills to learn.** how OAuth
2.0 authorization-code flow actually works (what `state` is for, why the code
is exchanged server-side, what a refresh token grants an attacker who steals
it); what a session cookie is and what `HttpOnly`/`SameSite` do; what CSRF is.
**You are about to hold other students' Google Classroom access.** This is the
one phase where shallow understanding is genuinely irresponsible. Spend a day
on the OAuth spec's authorization-code section and OWASP's session and CSRF
cheat sheets before writing anything. encryption library
choice and key rotation, FastAPI dependency injection. 
form handling, the settings UI.

**Risks.** (1) A missed `WHERE user_id = ?` — mitigated by cross-written
isolation tests and a dedicated review pass. (2) Underestimating this phase; it looks
like plumbing and behaves like a rewrite. (3) Being tempted to deploy
mid-phase. Don't.

**Exit criteria.** The isolation test suite passes, and independent attempts
to break it have failed.

---

## 8. PHASE 4 — HOSTED BACKEND: POSTGRES + REMOTE EXECUTION

**Purpose.** Ragra stops depending on one laptop being awake.

**Goal.** Ragra runs continuously on a server, against a managed Postgres,
reachable over HTTPS, with no machine of yours involved.

**Features.** None new. Users see nothing except that reminders now arrive at
3am.

**Technical work.**
- **SQLite → Postgres.** Your `repo.py` is hand-written SQL against stdlib `sqlite3`, which is good news: no ORM to fight. The porting work is real but mechanical — parameter style (`?` → `%s`), `AUTOINCREMENT` → `SERIAL`/`IDENTITY`, `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`, `datetime()` string comparisons → real `timestamptz`. **That last one is the trap:** you currently store ISO strings and compare them lexically, which happens to work in SQLite and is a genuine improvement in Postgres — but it changes comparison semantics everywhere reminders are selected. Treat it as a behaviour change with tests, not a type change.
- Keep `repo.py`'s function signatures identical. The abstraction that protects you already exists — every consumer calls `repo.*`, not SQL. Preserve that boundary and the migration is contained to one file.
- Data migration: your real SQLite DB → Postgres, one-time, verified by row counts and spot checks. Your personal history is worth preserving.
- Containerize (`Dockerfile`), deploy web + scheduled worker.

**Hosting recommendation — and why not Cloud Run.** You already investigated
Cloud Run Jobs + Cloud Scheduler and verified the free tier covers the
workload. It does. But it is now the wrong choice, for reasons your earlier
analysis correctly identified and that the pivot amplifies: it requires a
billing account with a payment method and has **no hard spending cap** — the
only "cap" is a budget alert that can disable the entire project. For two
students, an unbounded-downside bill on a project that now also serves a public
web app is a bad risk. It also splits your deployment across two services
(Cloud Run service for web + Cloud Run Job for the worker) with more IAM than
this deserves.

**Recommendation: a single small container platform running both the web app
and an in-process scheduler, plus a managed Postgres with a real free tier.**
Concretely: **Fly.io or Railway for the app; Neon or Supabase for Postgres.**
Rationale: one deployment artifact, one log stream, one config surface,
predictable or zero cost, and no billing cliff. Use APScheduler inside the web
process for the 15-minute tick — at 1–100 users this is entirely adequate and
removes an entire moving part. Split the worker out only when tick duration
starts affecting request latency, which is a real signal, not a guess.

Keep Cloud Run on the shortlist for one specific case: if you end up wanting
everything inside the Google Cloud project that already holds your OAuth
client. That is a legitimate reason. "It's Google's" is not.

**Database work.** Postgres schema (ported migrations), connection pooling, one-time data migration, automated backups + one **verified restore**.

**Frontend work.** None, beyond making URLs absolute and cookies `Secure`.

**Backend work.** Config from environment (already mostly true — `config.py` is clean here). Secrets from the platform's secret store. Structured logging to stdout. A `/health` endpoint. Graceful handling of a Google API outage across all users rather than one.

**Testing work.** The full suite against Postgres, not just SQLite — this is
the phase's real cost and it is easy to underestimate. Concurrent-writer test:
web POST during an active tick. Migration verified on a copy of real data.

**Security work.** TLS. Secrets never in the image or the repo. Postgres not
publicly reachable. Per-user Google API failures isolated. Rate limiting on
auth routes.

**Deployment work.** This *is* the phase.
**Dependencies.** Phase 3 complete. Do not deploy a single-user-shaped app.

**Definition of done.** Laptop off for 48 hours; reminders still arrive;
dashboard still reachable; a restore from backup has been performed at least
once into a scratch database.

**Duration.** 40–70 hours hands-on, 3–6 weeks calendar. **Widest error bars in
the roadmap** — first deployments always cost more than estimated, and neither
of you has done this before.

**Skills to learn.** basic Postgres operations
(connections, pooling, why a connection limit matters); what a container image
actually is; how environment-based secrets reach a running process; how to read
production logs. You cannot operate what you don't understand at 2am. Dockerfile authoring, platform-specific deploy config,
APScheduler. the mechanical SQL dialect port.

**Risks.** (1) The port takes 3× the estimate — likely; budget for it.
(2) Free-tier limits (Neon compute suspension, Fly machine sizing) surprising
you in production. (3) Deploying before Phase 3's isolation is proven.
(4) Losing your personal history in the data migration — mitigated by never
running it against the original.

**Exit criteria.** 48 hours laptop-off with correct behaviour, and a
successful restore drill.

---

## 9. PHASE 5 — WEB PUSH + NOTIFICATION PREFERENCES

**Purpose.** Give normal users a notification channel that requires no Hermes,
no WhatsApp, and no setup beyond clicking "Allow."

**Goal.** A student enables notifications once and receives reminders with the
browser closed, with email as the guaranteed fallback.

**Features.** Push subscription in the UI. PWA manifest and installability.
Per-category notification preferences (which reminder types, which channels,
quiet hours). Deep links from a notification to the relevant task. Delivery
status visible in-app.

**Technical work.**
- VAPID key pair generated once, private key in the platform secret store, public key served to the client.
- `pywebpush` server-side; a hand-written service worker (~80 lines) client-side. This is the only JavaScript in v1.
- `push_subscriptions` table (per user, multiple devices), storing endpoint + keys.
- `WebPushProvider` implementing the existing `NotificationProvider` protocol — **zero changes to `dispatch.py`.** This is the payoff for the Phase 1 `Notification` refactor.
- Routing policy from Q7: push when subscribed; email always for `FINAL_1H` and `DUE_TODAY`; dedupe by `dedupe_key` so a user with both channels doesn't get two identical alerts they perceive as spam.
- **`404`/`410` from a push endpoint → delete that subscription row.** Required, not optional; stale subscriptions otherwise accumulate forever and pollute your failure metrics.

**Database work.** `push_subscriptions`, `user_notification_preferences`.
**Frontend work.** Permission-request flow (with a real explanation before the browser prompt — asking cold gets denied), service worker, manifest, icons, preferences UI.
**Backend work.** `WebPushProvider`, routing policy, subscription lifecycle.
**Testing work.** Provider tested against a stubbed push endpoint (no network). `410` → row deleted. Preferences respected. Dedupe verified across both channels. Expired VAPID handled.
**Security work.** VAPID private key never leaves the server. Subscription endpoints are per-user secrets — never logged, never rendered. Notification payloads must not contain sensitive academic detail beyond what a lock-screen preview should show.
**Deployment work.** HTTPS is a hard requirement (satisfied by P4). Service worker served from the origin root.
**Dependencies.** P3 (identity, for subscription ownership) and P4 (HTTPS).

**Definition of done.** Reminder arrives on a phone with the browser closed;
disabling push in preferences stops it; email still arrives for critical
reminders.

**Duration.** 25–45 hours hands-on, 2–4 weeks calendar.

**Skills to learn.** what a service worker is and
its lifecycle, and the platform reality that **iOS Safari only delivers push to
installed PWAs** — this constrains your onboarding UX, not just your code.
VAPID, `pywebpush`, subscription management. manifest, icons, boilerplate.

**Risks.** (1) iOS limitations disappointing users — set expectations in the UI
rather than fighting the platform. (2) Notification fatigue — your reminder
cadence (up to 5 per task) was designed for one channel and one user; re-tune
it before external users see it. (3) Silent subscription expiry.

**Exit criteria.** Push works on both a real Android and a real iOS device, or
its iOS limitation is documented and communicated in-product.

---

## 10. PHASE 6 — PILOT: 1–3 REAL USERS

**Purpose.** Find out what is actually wrong. Not by reasoning — by watching
three people who aren't you use it.

**Goal.** Three FAST students use Ragra for a full month and prefer it to
checking Classroom.

**Features.** Onboarding: sign in → authorize Classroom → set section/program →
enable notifications → see today. This flow does not currently exist in any
form; everything before now assumed a user who already had a configured
database. **Budget for it as a real feature, not a wrapper.**

Plus: a "something is wrong" reporting path, and an admin view for you to see
per-user sync health without reading their data.

**Technical work.** Onboarding flow. Per-user timetable enrollment entry (the
section/course configuration that is currently your hand-edited
`enrollment.py` — a real UI now). Empty states everywhere. Error states for
"Classroom authorization expired." Per-user sync-health surfacing.

**Database work.** Whatever the pilot reveals. Expect at least one migration
you didn't plan.
**Frontend work.** Onboarding, empty states, error states, help text. Probably a visual pass — this is when Ragra is first seen by someone who won't forgive an ugly, confusing screen.
**Backend work.** Graceful degradation when one user's Google auth breaks. Per-user rate limiting of Classroom API calls.
**Testing work.** Real users are the test. Add regression tests for everything they break.
**Security work.** Privacy policy (required for Google verification in P7 anyway — write it now). Account deletion actually verified by a real user deleting a real account. Confirm you cannot accidentally read pilot users' academic content while debugging; log data-minimisation matters here.
**Deployment work.** Monitoring: an alert when a user's sync fails repeatedly. Your existing `pipeline_health` self-alerting is the right foundation — make it per-user.
**Dependencies.** P5.

**Definition of done.** Three users, one month, and every §25 success criterion
demonstrably met for people who are not you.

**Duration.** 25–40 hours hands-on, 3–5 weeks calendar — calendar-dominated,
because you are waiting on humans.

**Skills to learn.** reading production logs and
diagnosing from incomplete information — the core operational skill, and the
one that decides whether Ragra survives having users. 
monitoring setup. UI copy, empty states.

**Risks.** (1) Onboarding friction — most likely cause of pilot failure; if
someone can't get to a useful screen in five minutes they stop. (2) The FAST
sheet changing mid-pilot. (3) Discovering the relevance engine is wrong on
sections you never saw — **expected, and the reason it fails open.**

**Exit criteria.** A month of use, a fixed bug list, and at least one user
saying they'd be annoyed to lose it. If nobody says that, do not proceed to
P7 — fix the product instead.

---

## 11. PHASE 7 — PRODUCTION HARDENING AND V1 LAUNCH

**Purpose.** Go from "works for three people I know" to "works for strangers."

**Goal.** Public v1 for FAST-NUCES students.

**Features.** No new user features. Anything you're tempted to add here belongs
in P8.

**Technical work.** Load-testing at a realistic multiple of expected users.
Per-user Classroom API quota management — at 100 users with 8 courses each, a
15-minute tick is ~19,200 API calls/hour against a shared project quota;
implement adaptive intervals (sync active courses often, quiet ones rarely)
**before** launch, not after you're throttled. Data export. Account deletion
hardening. A runbook: what to do when Google is down, when the sheet changes,
when the database fills.

**⚠️ Google OAuth verification — the gate you have not accounted for.** Your
Classroom scopes are *sensitive*. Until the app is verified, you are capped at
**100 test users**, and in Testing publishing status refresh tokens expire
after 7 days (which your own `PROJECT_STATUS.md` records having worried about).
Verification requires a homepage, a privacy policy, a demo video, a domain you
own and have verified, and Google's review — **2 to 6 weeks of calendar time
you do not control**, and rejections cost another round trip. Start this at the
*beginning* of P7, in parallel with everything else. It is the single most
likely cause of a launch slipping by a month, and no amount of engineering
velocity shortens it.

**Database work.** Index review under real query patterns. Retention policy for `notification_deliveries` and history.
**Frontend work.** A real visual pass. Landing page (required for verification anyway). Mobile layout.
**Backend work.** Rate limiting, abuse prevention, error tracking.
**Testing work.** Load test. Security review. Full restore drill under time pressure.
**Security work.** Full adversarial review: authz on every route, injection, session handling, secret handling, dependency audit. Budget a dedicated deep-review pass plus, ideally, one outside human who has done this before.
**Deployment work.** Domain, TLS, monitoring, alerting, a documented rollback.
**Dependencies.** P6 exit criteria genuinely met.

**Definition of done.** A stranger from FAST can sign up and use Ragra without
contacting you, and you can go a week without touching the server.

**Duration.** 30–50 hours hands-on, **4–8 weeks calendar** — dominated by the
Google review wait.

**Skills to learn.** what data you hold, where,
for how long, and how you'd delete it on request — you cannot write an honest
privacy policy otherwise, and Google will ask. load
testing, rate limiting. boilerplate.

**Risks.** (1) Google verification rejected or slow — **the top schedule risk
in this roadmap.** (2) A security issue surfacing after launch. (3) The
100-user cap arriving before verification clears.

**Exit criteria.** Verified app, security review complete, runbook written, one
week hands-off.

---

## 12. PHASE 8 — POST-V1 (EXPLICITLY NOT V1)

Optional AI (announcement summarization, prioritization, natural-language
task creation) — strictly additive, core still works without it, and the
existing `test_ai_isolation.py` boundary must hold. WhatsApp Cloud API. Apple
Calendar / ICS feed. Semester analytics. Multi-university support (a large
project: the relevance engine and timetable adapter are both FAST-specific).
Billing. Mobile apps. None of this is v1, and none of it should be started
before P7 exits.

---

## 13. V1 SCOPE (§24)

**MUST HAVE FOR V1**

| Item | Phase | Why non-negotiable |
|---|---|---|
| Google Classroom read-only sync | ✅ done | The product |
| FAST timetable sync | ✅ done | The differentiator |
| Deterministic reminder engine | ✅ done | The product |
| Relevance filtering | P1–P2 | The other differentiator |
| Official deadline tracking + change detection | ✅ / P2 | Core promise |
| Personal tasks | P2 | Half of the daily-use loop |
| Announcement workflow | P2 | Your stated core workflow |
| Class-aware reminders | P2 | Highest-value use of data you already have |
| Web dashboard | P2 | The product surface, not a debug view |
| Google Login + user isolation | P3 | Cannot have users without it |
| Hosted DB + remote execution | P4 | Laptop-off is disqualifying |
| Email notifications | P1 | The reliable channel |
| Web Push | P5 | The expected channel |
| Backups + monitoring | P4/P6 | You hold other people's academic lives |
| Account deletion + privacy policy | P6/P7 | Legal and Google requirement |

**GOOD TO HAVE AFTER V1.** Google Calendar sync (already built — keep it, but
it's not why anyone signs up), AI summarization, analytics, ICS/Apple Calendar,
richer attachment/material views, snooze, multi-device push management.

**NOT PART OF V1.** WhatsApp Cloud API. Advanced AI / chat. Billing. Public
scaling beyond a few hundred users. Enterprise infrastructure. Multi-university.
Mobile apps. Telegram (permanently — your own decision, and correct).

**The one thing already built that isn't in v1's critical path:** Google
Calendar sync. It works, it's tested, it costs nothing to keep, and it makes
zero difference to whether a new student adopts Ragra. Don't invest further in
it before P8.

---

## 14. SUCCESS CRITERIA — WHAT "V1 IS SUCCESSFUL" MEANS (§25)

Not "the code is done." These, in user terms:

1. A FAST student signs in with Google and reaches a useful screen in under 5 minutes, without contacting you.
2. Ragra reads their Classroom and shows their coursework.
3. Content belonging to another section is not proactively pushed at them — and is still *visible* if they look.
4. Official Classroom deadlines are never altered by Ragra, and are shown as authoritative.
5. When a teacher moves a deadline, the student is told, and reminders move.
6. They can create, reschedule, and complete their own tasks.
7. They can see today's classes with times and rooms.
8. They get a reminder before class.
9. What the dashboard shows and what the notifications say never disagree.
10. Notifications arrive with no local installation, agent, or WhatsApp setup.
11. Reminders arrive when the student's laptop is off.
12. No user can see another user's data — proven by tests, not asserted.
13. A failed notification channel never corrupts academic state and recovers on its own.
14. You can go a week without touching the server.

Ragra is v1 when all fourteen are true for people you have never met.

---

## 15. CONTRIBUTION WORKFLOW

**Branching.** `main` always green. Short-lived feature branches
(`feat/relevance-engine`, `feat/migrations`, `feat/user-id-tenant-key`). One
task per branch, days not weeks. PR + review, always — it's the mechanism that catches a boundary
violation before it reaches `main`. Rebase before opening. **No long-lived `personal` branch, ever** — the
personal edition is `.env` values and an optional provider, nothing more.

**Merge hotspots, in order of danger.**

| File | Danger | Rule |
|---|---|---|
| `db/repo.py` | Highest. 801 lines, everything touches it | Append within sections. Never reorder. Never two open PRs at once |
| `web/app.py` | High from P2 | Split into routers past ~300 lines. Dashboard restructure is single-owner |
| `sync/classroom_sync.py` | Medium in P2 | Relevance wiring and deadline-change notification both touch it — sequence them |
| `config.py` | Medium, grows every phase | Append only |
| `adapters/notify.py` | Low after P1 freezes it | Frozen post-P1; changes require explicit agreement |
| Migrations | **None by construction** | Numbered append-only files never conflict textually. Half the reason they're P1 |

**Phase-level rule.** Two phases have a single-owner exclusive task where
nothing else merges: the `user_id` tenant-key migration (P3) and the Postgres
port (P4). Treat those as stop-the-world. Everything else is parallelizable
with sequencing.

---

## 16. ESTIMATES

Hands-on = actual keyboard hours. Calendar = elapsed wall-clock for two
part-time students with coursework.

| Phase | Hands-on | Calendar |
|---|---|---|
| P0 Hygiene | 3–6 h | 1–2 days |
| P1 Core intelligence (M1) | 15–25 h | 1.5–3 wk |
| P2 Complete single-user (M2) | 40–70 h | 3–6 wk |
| P3 Identity + isolation | 35–60 h | 3–5 wk |
| P4 Postgres + hosting | 40–70 h | 3–6 wk |
| P5 Web Push | 25–45 h | 2–4 wk |
| P6 Pilot | 25–40 h | 3–5 wk |
| P7 Hardening + launch | 30–50 h | 4–8 wk |
| **Total to v1** | **215–365 h** | **5–9 months** |

**FASTEST PLAUSIBLE PATH — ~4.5 months.** Assumes: 10+ hours/week of
consistent engineering time; no exam period interrupts; the Postgres port hits
the low estimate; Google verification clears on the first submission in ~2
weeks; the pilot finds only small bugs; no FAST sheet restructure. Every one of
these is plausible. All of them together is not. Plan against it only if you
have a hard deadline and are willing to cut P5 (ship email-only v1).

**REALISTIC PATH — ~7 months.** Assumes: 6–8 hours/week, multi-week
interruptions for exams, the Postgres port runs over, Google verification takes
one round trip (~4 weeks), the pilot surfaces two or three real design
problems. **This is what I'd plan against.**

**CONSERVATIVE PATH — ~11–12 months.** Assumes: sporadic work, one long
dormancy, a Google verification rejection, a FAST timetable restructure forcing
adapter rework, and one phase (most likely P4) taking triple its estimate.
Nothing here is unusual for a student side project. The main defence is that
P2 leaves you with something genuinely useful to yourself — so a dormancy
costs momentum, not value.

---

## 17. CURRENT POSITION (§27)

```
CURRENT PHASE:                    Phase 2 — Complete the Single-User Product
                                  (implementation complete; two-week soak
                                  test outstanding — see exit criteria)
CURRENT MILESTONE:                M2
PHASE 1 STATUS:                   COMPLETE. Landed as three
                                  logically separate, independently
                                  reviewed commits (migration framework,
                                  notification value object, EmailProvider)
                                  on top of the relevance engine + profile
                                  work already done. Suite: 290 passing
                                  (was 211 at Phase 0 close). See
                                  docs/PROJECT_STATUS.md for the full
                                  verified snapshot.
CURRENT TASK — MAINTAINER:        Phase 2 implemented end to end by the
                                  sole active developer, covering both
                                  originally-allocated tracks. Remaining
                                  before Phase 2 can be called complete:
                                  the two-week real-use soak test that is
                                  this phase's actual exit criterion, and
                                  the deferred HTMX pass (plain forms ship
                                  today and work).
NEXT MILESTONE AFTER PHASE 2:     M3 — Phase 3, identity and user isolation
NEXT MAJOR PRODUCT MILESTONE:     End of Phase 2 — Ragra is genuinely useful to you
FIRST POINT REAL USERS CAN USE IT: End of Phase 6 (pilot), ~4–7 months out
FIRST POINT IT IS A REAL HOSTED PRODUCT: End of Phase 4
PUBLIC V1 DEFINITION:             End of Phase 7 — all 14 criteria in §14 true
                                  for strangers; Google-verified; hands-off for
                                  a week
```

---

## 18. EXECUTIVE SUMMARY

**1. Where Ragra is today.** A genuinely well-engineered single-user pipeline,
211 tests green, running every 15 minutes on one Windows laptop. Classroom,
Calendar, and FAST timetable all sync idempotently. The reminder engine is pure
and deterministic. The notification boundary and AI isolation are already the
right shapes — better than most projects at this stage. It has **no user model,
no login, no hosting, no relevance filtering, no personal tasks, and no way for
anyone but you to use it.** It also cannot install cleanly from a fresh clone.

**2. What M1 gives you.** The one piece of logic that's actually Ragra's
(deterministic, fail-open relevance) and a second real notification channel.
No user-visible change — deliberately. It also gives you migrations, without
which every later schema change is a hand-rolled hazard.

**3. What M2 gives you.** The moment Ragra becomes a product. Personal tasks,
announcement workflow, today's classes and rooms (data you already collect and
currently never read), class-aware reminders, deadline-change notifications, a
real dashboard. At the end of M2 you should stop opening Classroom manually.

**4. Phase count.** 9 (P0–P8). Seven to v1; P8 is explicitly post-v1.

**5. Total hands-on effort to v1.** 215–365 hours.

**6. Earliest realistic point for 1–3 users.** End of Phase 6: ~4 months
fastest, ~5–6 months realistic.

**7. Earliest realistic point for public v1.** End of Phase 7: ~4.5 months
fastest, **~7 months realistic**, gated at the end by a Google review you don't
control.

**8. Three biggest technical risks.**
- **The Postgres port and the tenant-key migration (P3–P4).** Two stop-the-world changes touching every query, with data-loss and data-leak failure modes that fail silently. Mitigation: single owner, a dedicated deep-review pass, verified against copies of real data, cross-written isolation tests.
- **Timezone semantics in class-aware reminders (P2).** Timetable times are local wall-clock; everything else is UTC ISO strings compared lexically. That mismatch will produce wrong-time reminders that pass tests written by whoever made the mistake. Decide it explicitly, in writing, before coding.
- **The FAST timetable source.** A public spreadsheet you don't control, feeding one of your two differentiators. Your adapter is admirably defensive already — no hardcoded GIDs, rows, or colors — but a structural change mid-semester breaks a live product for real users. Treat "the sheet changed shape" as an operational event with an alert, not an exception.

**9. Three biggest product risks.**
- **Google OAuth verification (P7).** Sensitive Classroom scopes cap you at 100 users and expire refresh tokens weekly until verified. 2–6 weeks of review you don't control, plus a homepage, privacy policy, verified domain, and demo video. Start it at the beginning of P7, not the end. This is the most likely cause of a month's slip.
- **Nobody wants it.** You are ~5 months of work from finding out whether students other than you will use this. Nothing in P3–P5 tests that hypothesis. **Consider talking to three FAST students during P2** — show them your working single-user version, watch their reaction, and let it change P2's priorities. That costs a week and could save three months.
- **Notification fatigue.** The current cadence (up to 5 reminders per task) was tuned for one user who tolerates it. Add push and email and it becomes spam to a stranger. Re-tune before P6, not after.

**10. Next action.** See §17 for current phase status and §4-§12 for phase-by-phase technical work.
