# Ragra Interface Contracts — Phase 0→1 Freeze

This document defines four contracts that Phase 1 and Phase 2 depend on. These signatures remain stable throughout v1; implementation details and providers may vary.

---

## 1. NotificationProvider Protocol

**Location:** `ragra/adapters/notify.py`

**Contract:**
```python
class NotificationProvider(Protocol):
    def send(self, message: str) -> NotifyResult: ...

@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    error: str | None = None
```

**Semantics:**
- Every provider implements exactly one method: `send(message: str) -> NotifyResult`.
- A provider must report success or failure; it never decides whether the caller should retry.
- Idempotency (never resending the same message) is the caller's responsibility.
- Multiple providers may be configured; `dispatch.py` fan-outs to all (one success = message sent).
- An empty provider list is a valid, stable state (reminders stay PENDING).

**Current Implementations:**
- `HermesProvider` — optional, shells out to `hermes send` (current only concrete provider)
- Planned: `EmailProvider` (Phase 1), `WebPushProvider` (Phase 5)

---

## 2. Notification Value Object (Phase 1 Refactor)

**Location:** `ragra/reminders/dispatch.py`, `ragra/health.py`

**Current Signature (to be refactored):**
```python
def send(message: str) -> NotifyResult
```

**Proposed Signature (Phase 1):**
```python
@dataclass(frozen=True)
class Notification:
    text: str
    reminder_id: int | None = None  # For delivery tracking
    category: str | None = None     # e.g., FINAL_1H, DUE_TODAY, for routing policy

def send(notification: Notification) -> NotifyResult
```

**Why:** Enables per-category routing (email vs push policy), delivery tracking, and deduplication across providers without changing `dispatch.py`'s overall structure.

**Guarantee:** This refactor is internal to the notification layer. `dispatch.py` and `health.py` never import provider-specific code; they only depend on `NotificationProvider.send()`.

---

## 3. Relevance Decision (Planned Phase 1)

**Location:** `ragra/relevance/engine.py` (to be created)

**Proposed Signature:**
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

**Five Ambiguous Cases (decided, not implemented yet):**
1. Chapter references ("Section 3 of the textbook") → `UNKNOWN` (not a section label)
2. Course codes ("CS-101") → `UNKNOWN` (code-shaped but ambiguous context)
3. Ranges ("Sections A-D") → `UNKNOWN` (do not expand to individual matches)
4. No section token at all → `UNKNOWN` (could be for all students)
5. Title/description disagree → `UNKNOWN` (cannot resolve)

**Property Test Invariant (non-negotiable):**
> No input ever yields `notify=False` except `OTHER_SECTION`.

This invariant is enforced by test: if a future edge case breaks it, the test fails before shipping.

---

## 4. UserAcademicProfile

**Location:** `ragra/relevance/profile.py` (to be created)

**Proposed Signature:**
```python
@dataclass
class UserAcademicProfile:
    program: str                    # e.g., "CS", "SE", "EE"
    expected_semester: int          # derived, descriptive only - see below
    enrolled_courses: list[str]     # FAST enrollment course names, e.g. ["OOP Theory", "DLD"]
    section_labels: dict[str, str]  # {course_name: "CS-A", "CS-B", "CS-C", ...}
    enrollment_config: dict         # Raw FAST enrollment rules (internal)

def load_profile(user_id: str | None = None, *, today: date | None = None) -> UserAcademicProfile:
    """Load the academic profile for this user (or the default user if None).
    `today` is injectable for deterministic testing of expected_semester;
    defaults to date.today()."""
```

**Semantics:**
- **Phase 0→1 (single-user):** `user_id` is accepted but ignored; returns hardcoded profile from `ragra/timetable/enrollment.py`.
- **Phase 3 (multi-user):** `user_id` fetches a row from `user_profiles` table; same signature, different source.
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

**Invariant (enforced by a write-guard test, not just documentation):**
```python
def update_task_personal_deadline(..., task_id: int, ...) -> None:
    """Raises if task_id is Classroom-sourced (not manual)."""

def cancel_task(..., task_id: int, ...) -> None:
    """Raises if task_id is Classroom-sourced."""
```

**Guarantee:**
- Classroom tasks are read-only to the user (no reschedule, no cancel, no edit).
- Manual tasks can be edited, rescheduled, or cancelled by the user.
- No API ever accepts `source_type` or `external_id` as a parameter; they are derived from context.
- The `__personal__` pseudo-course (created at schema-init time) holds all manual tasks.

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
- **UserAcademicProfile:** Can add fields in Phase 3+ for multi-user; signature stays stable. New fields must be in the dataclass, not as hardcoded per-user constants.
- **Task Source Boundary:** Never. The write-guard test must hold through all phases. Classroom tasks remain read-only forever.