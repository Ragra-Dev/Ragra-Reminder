# Ragra Interface Contracts — Phase 0→1 Freeze

This document defines four contracts that Phase 1 and Phase 2 depend on. These signatures remain stable throughout v1; implementation details and providers may vary.

---

## 1. NotificationProvider Protocol

**Location:** `ragra/adapters/notify.py`

**Status: implemented (Phase 1).**

**Contract:**
```python
class NotificationProvider(Protocol):
    def send(self, notification: Notification) -> NotifyResult: ...

@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    error: str | None = None
```

**Semantics:**
- Every provider implements exactly one method: `send(notification: Notification) -> NotifyResult` (see contract #2 for `Notification`).
- A provider must report success or failure; it never decides whether the caller should retry.
- Idempotency (never resending the same message) is the caller's responsibility.
- Multiple providers may be configured; `dispatch.py` fan-outs to all (one success = message sent).
- An empty provider list is a valid, stable state (reminders stay PENDING).

**Current Implementations:**
- `HermesProvider` — optional, advanced-personal-only, shells out to `hermes send`. Never required.
- `EmailProvider` — optional, speaks SMTP directly via the standard library (`smtplib`/`email.message`, no third-party dependency). Credentials from environment only (`RAGRA_SMTP_*`); never appear in a `NotifyResult.error` string (see `_redact()`).
- Planned: `WebPushProvider` (Phase 5).

---

## 2. Notification Value Object

**Location:** `ragra/adapters/notify.py` (dataclass), consumed by `ragra/reminders/dispatch.py`, `ragra/health.py`

**Status: implemented (Phase 1).**

**Signature:**
```python
@dataclass(frozen=True)
class Notification:
    text: str
    reminder_id: int | None = None  # For delivery tracking
    category: str | None = None     # e.g., T_MINUS_1D, HEALTH_ALERT, for routing policy

def send(notification: Notification) -> NotifyResult
```

**Why:** Enables per-category routing (email vs push policy), delivery tracking, and deduplication across providers without changing `dispatch.py`'s overall structure. `dispatch.py` sets `category` to the reminder's `reminder_type`; `health.py` sets it to `"HEALTH_ALERT"`.

**Guarantee:** This refactor is internal to the notification layer. `dispatch.py` and `health.py` never import provider-specific code; they only depend on `NotificationProvider.send()`.

---

## 3. Relevance Decision

**Location:** `ragra/relevance/engine.py`

**Status: implemented (Phase 1).**

**Signature:**
```python
from enum import Enum

class RelevanceDecision(Enum):
    RELEVANT = "relevant"           # Content is for this student
    OTHER_SECTION = "other_section" # Content is explicitly for a different section
    UNKNOWN = "unknown"             # Ambiguous; cannot determine confidently

def is_relevant(
    content_title: str,
    content_description: str,
    course_name: str,
    profile: UserAcademicProfile
) -> RelevanceDecision:
    """Determine whether this Classroom content belongs to this student."""
```

**Semantics:**
- **RELEVANT:** Notify (default). Example: "Lab 02 Section C" matches profile `CS-C`.
- **OTHER_SECTION:** Do not notify proactively, but keep the task visible in dashboard (fail-open). Example: "Lab 02 Section D" when profile is `CS-C`.
- **UNKNOWN:** Do not suppress (notify). Example: ambiguous titles, or conflicting evidence in title vs description.
- Never invented data: **no inference, expansion, or AI judgment.** Only pattern matching against the profile.

**Five Ambiguous Cases (decided, implemented):**
1. Chapter references ("Section 3 of the textbook") → `UNKNOWN` (not a section label)
2. Course codes ("CS-101") → `UNKNOWN` (code-shaped but ambiguous context)
3. Ranges ("Sections A-D") → `UNKNOWN` (do not expand to individual matches)
4. No section token at all → `UNKNOWN` (could be for all students)
5. Title/description disagree → `UNKNOWN` (cannot resolve)

**Sixth case, discovered during implementation, decided the same way relevance always is — never suppress on a positive signal:**
6. "All sections"/"all batches" bypass phrases (e.g. "Announcement for all sections") → `RELEVANT`, not merely `UNKNOWN`. This is an explicit, unambiguous positive signal that content applies regardless of section — stronger than "no evidence", so it is classified as confirmed-relevant rather than merely not-suppressed. Detected in `ragra/relevance/sections.py`'s `extract_sections()` (`applies_to_all` flag), checked before section-token extraction.

**Property Test Invariant (non-negotiable):**
> No input ever yields `notify=False` except `OTHER_SECTION`.

This invariant is enforced by test: if a future edge case breaks it, the test fails before shipping.

---

## 4. UserAcademicProfile

**Location:** `ragra/relevance/profile.py`

**Status: implemented (Phase 1).**

**Signature:**
```python
@dataclass
class UserAcademicProfile:
    program: str                    # e.g., "CS", "SE", "EE"
    expected_semester: int          # derived, descriptive only - see below
    enrolled_courses: list[str]     # FAST enrollment course names, e.g. ["OOP Theory", "DLD"]
    section_labels: dict[str, str]  # {course_name: "CS-A", "CS-B", "CS-C", ...}
    enrollment_config: dict         # Raw FAST enrollment rules (internal)

def load_profile(
    conn: sqlite3.Connection | None = None,
    *,
    user_id: int | None = None,
    today: date | None = None,
) -> UserAcademicProfile:
    """The academic profile for one user.
    `today` is injectable for deterministic testing of expected_semester;
    defaults to date.today()."""
```

**Semantics:**
- **Phase 0→1 (single-user):** `user_id` was accepted but ignored; returned the hardcoded profile from `ragra/timetable/enrollment.py`.
- **Phase 3 (multi-user, implemented):** the profile is a `user_profiles` row (migration 0024). `user_id` narrowed from `str` to `int` to match the tenant key used by every other table, and `conn` was added because the profile now lives in the database rather than in a module. Both are optional so callers that genuinely have no database — the relevance engine's own tests — keep working unchanged.
- **The fallback is deliberately narrow.** A user with no stored profile gets the module default *only* if they are the pre-identity owner, the account whose configuration has always lived in `ragra/timetable/enrollment.py`. Everyone else gets an empty profile. That degrades safely in both consumers: relevance falls open and suppresses nothing, and timetable matching finds no classes rather than somebody else's. Silently handing a new user another person's enrollment is the far worse failure, and it is the one a permissive default produces.
- **Adoption is ordered, and the order matters.** Linking the owner's account is exactly what stops it matching that fallback, so the profile is written as a real row *before* the account is linked (see `adopt_legacy_profile`). Reversing those two steps would silently empty the owner's enrollment on their first sign-in.
- No consumer ever imports `MY_ENROLLMENT` as a module constant again.
- `enrolled_courses`/`section_labels` are keyed by FAST's own course names (`ragra/timetable/enrollment.py`), not Classroom course codes — no crosswalk between the two systems exists yet. An earlier draft of this contract illustrated Classroom-style codes here; corrected to match the only real, non-invented data source available in Phase 0→1.

**`expected_semester` — naming and derivation (decided before implementation, this phase):**
- Originally drafted as `current_semester`; renamed because "current" implies an authoritative, actual fact, and this value is neither. It is computed, at `load_profile()` time, from hand-edited `ENROLLMENT_START_YEAR` / `ENROLLMENT_START_TERM` constants in `ragra/timetable/enrollment.py` plus the current date — never hardcoded as a bare integer that goes stale.
- **Purely descriptive/contextual metadata.** It means "the semester this student's cohort would nominally be in," not "the semester whose courses this student is actually taking."
- **Hard constraint, enforced by test:** `expected_semester` is never read by `is_relevant()` and never used to filter, suppress, or decide eligibility for a Classroom-enrolled course or its content. A student may be enrolled in a course from an earlier catalog semester (frozen semester, repeat, delayed prerequisite, etc.), and that course remains fully relevant regardless of `expected_semester`.
- Authoritative course eligibility remains, unchanged: (1) active Classroom enrollment (Ragra only ever sees courses Classroom returns for this account) plus an explicit opt-out exclude list, and (2) `is_relevant()`'s section-level matching for courses Classroom bundles into multiple sections. `expected_semester` is not a third eligibility signal and must never become one.

**Contracts:**
- Every sync stage calls `load_profile()` at the start; never reads enrollment from module-level state.
- Relevance engine calls `load_profile()` at comparison time (for potential future per-user config).
- Timetable sync uses the profile's `enrolled_courses` and `section_labels` to match rows.

**`RawProfile` / `load_raw_profile()` (added Phase 3, for the `/account` editor):** a second, separate read path that returns the profile's stored fields exactly as saved — `program`, `batch_year`, `enrollment_start_year`, `enrollment_start_term`, `enrollment` — as opposed to `UserAcademicProfile`'s *derived* shape, which computes `expected_semester` and does not carry the raw start year/term at all once they have been used. An editor needs to redisplay what a user actually saved, including fields `load_profile()` deliberately discards after consuming them; adding that need to `UserAcademicProfile` itself would have broken the "signature stays stable" commitment above. `RawProfile` has no consumer outside `ragra/web/app.py`'s account routes and carries no eligibility semantics of its own — it is a form-prefill read, not a second relevance/matching input.

---

## 5. Task Source Boundary (Manual vs Classroom)

**Location:** `ragra/db/repo.py` — write-guard functions

**Existing Schema:**
```sql
CREATE TABLE tasks (
    source_type TEXT NOT NULL,  -- coursework | announcement | material | manual
    external_id TEXT,           -- NULL for manual tasks
    ...
)
```

**Amended in Phase 2.** The original draft of this contract said "Classroom tasks are read-only to the user (no reschedule, no cancel, no edit)" and required `update_task_personal_deadline` to raise for Classroom-sourced tasks. Taken literally that would have deleted a correct, shipped feature: setting a *personal* completion target on a Classroom assignment is the entire purpose of `personal_deadline` (`docs/DOMAIN.md`: "`personal_deadline` is the user's intended completion time"), and it never touches Classroom's data. The real invariant being protected is **"Classroom-authoritative data is never user-writable"**, not "rows sourced from Classroom are frozen". The contract below states that precisely.

**Field ownership (the actual invariant):**

| Field group | Fields | Writable by |
|---|---|---|
| Classroom-authoritative | `title`, `description`, `link`, `actual_deadline`, `kind`, `course_id`, `source_type`, `external_id`, `source_published_at`, `source_updated_at` | Classroom sync only — **never** a user-facing API, for any task |
| Ragra-owned | `personal_deadline`, completion status | The user, on **any** task (Classroom-sourced or manual) |
| Existence | cancellation | The user on **manual** tasks only; for Classroom-sourced tasks, existence is Classroom's to decide (`cancel_tasks_missing_from_source` owns that transition) |

**Invariant (enforced by write-guard tests, not just documentation):**
```python
def set_personal_deadline(..., task_id: int, ...) -> None:
    """Allowed for every task. personal_deadline is Ragra-owned."""

def mark_completed(..., task_id: int, ...) -> None:
    """Allowed for every task. Completion is Ragra-owned."""

def cancel_task(..., task_id: int, ...) -> None:
    """Raises TaskSourceViolation if task_id is Classroom-sourced."""

def update_manual_task(..., task_id: int, ...) -> None:
    """Raises TaskSourceViolation if task_id is not source_type='manual'."""
```

**Guarantee:**
- No Classroom-authoritative field is writable through any user-facing API, on any task.
- Manual tasks can be edited, rescheduled, or cancelled by the user; Classroom-sourced tasks can be planned (personal deadline) and completed, but never edited or cancelled.
- No API ever accepts `source_type` or `external_id` as a parameter; they are derived from context.
- The `__personal__` pseudo-course (created at schema-init time) holds all manual tasks.

**Write-guard defense in depth (Phase 2 security design).** There is no auth layer behind these routes, so the boundary is enforced at three independent levels:
1. **Route signatures are explicit.** Every route declares each accepted `Form(...)` parameter by name. No dict-splatting, no `**kwargs`, no request-body model that permits extra fields — a route that only declares `personal_deadline` is structurally incapable of receiving `title`. This mirrors the existing "structurally true, not just a rule" approach used for read-only Classroom access (`ClassroomGoogleClient` has no write method to call).
2. **Repo-layer guards.** `cancel_task`/`update_manual_task` re-check `source_type` and raise `TaskSourceViolation`, so a future route bug cannot corrupt authoritative data.
3. **Input validation before write.** Values are parsed/validated (e.g. a personal deadline must parse as a real date) rather than stored raw.

Enforced by adversarial tests that POST unexpected extra fields (`title`, `actual_deadline`, `source_type`, `external_id`) to every personal-task route and assert every Classroom-authoritative field is byte-identical afterward.

---

## Phase 1 Implementation Checklist

Before implementation starts, confirm:

- [ ] Four contracts above are read and understood
- [ ] Five ambiguous-corpus cases are accepted as written
- [ ] Notification refactor scope is clear (message string → Notification object, internal to dispatch)
- [ ] UserAcademicProfile signature is locked; no additional fields without re-negotiating
- [ ] Task source boundary write-guard test is written first (guard the invariant before implementation)

---

## When These Contracts Change

- **NotificationProvider:** Never — it is the foundation of the pluggable architecture. Adding fields to `NotifyResult` requires cross-developer review (affects dispatch logic).
- **Relevance Decision:** Never in v1. New cases must go through the five-case decision table, not improvised in code.
- **UserAcademicProfile:** Can add fields in Phase 3+ for multi-user; the dataclass shape has stayed stable. `load_profile`'s *signature* did change in Phase 3 (a `conn` parameter, and `user_id` narrowed from `str` to `int`), which this contract anticipated as "different source, same signature" and which turned out not to be achievable — a profile that lives in the database needs a connection to read it. Recorded here rather than glossed over. New fields must be in the dataclass, not as hardcoded per-user constants.
- **Task Source Boundary:** Never. The write-guard test must hold through all phases. Classroom tasks remain read-only forever.

---

## 6. Ownership (Phase 3 freeze)

**Status: implemented (Phase 3).**

The invariant every other Phase 3 contract rests on: **every row belongs to
exactly one account, and every query names the account it is acting for.**

**Contracts:**

- Every user-owned table carries `user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE`. There is no nullable-owner state and no shared row.
- Every repository function that touches such a table takes a required `user_id` keyword and filters on it. This is enforced structurally by `tests/test_user_scoping_guard.py`, which walks the source and fails if any function queries a user-owned table without naming an owner — because the way this property decays is not an existing query changing, it is a *new* function next year that forgets.
- Exactly two exemption markers exist, and their total number is itself asserted so it cannot drift:
  - `ragra:cross-user` — retention housekeeping that deletes strictly by age and returns only a count. Scoping it per user would leave a departed account's rows behind forever.
  - `ragra:token-scoped` — keyed by an unguessable secret rather than an owner. Session lookup is the only case: resolving the owner is what it does.
- **Identity is the Google `sub`, never the email address.** An account's email can change while its subject id cannot, so email matching would let a reassigned address inherit somebody else's data. Email is used for exactly one decision — who may claim the pre-identity owner row, once — and that is configured out of band.
- **Uniqueness is per-user, not global.** `courses.external_id`, `reminders.idempotency_key`, `calendar_events.google_event_id` and `timetable_events.external_id` are unique *within* an account. Classmates legitimately share a Classroom course id and a timetable slot; a global constraint would silently reject the second user's row, or suppress their reminder entirely.
- **A rejected cross-account write leaves no trace.** History is recorded from the update's own rowcount, never unconditionally, so a blocked write does not append a false entry to the victim's audit trail.
- **Account deletion is complete or it is loud.** Completeness comes from `ON DELETE CASCADE` on every owned table, verified by walking the schema rather than a written-down list; a leftover row raises rather than reporting success.

**When this changes:** never. A new table carrying user data must carry
`user_id NOT NULL` with a cascade, and must be added to the guard's table
list — the guard's schema-completeness test fails until it is.
