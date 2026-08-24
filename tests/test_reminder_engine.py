from datetime import datetime, timedelta, timezone

from ragra.reminders.engine import compute_reminder_plan


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_normal_task_gets_full_countdown():
    discovered = _dt("2026-09-01T09:00:00+00:00")
    deadline = _dt("2026-09-10T23:59:00+00:00")
    plan = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    types = [p.reminder_type for p in plan]
    assert "T_MINUS_3D" in types
    assert "T_MINUS_2D" in types
    assert "T_MINUS_1D" in types
    assert "DUE_TODAY" in types
    assert "FINAL_1H" in types
    for p in plan:
        assert discovered <= p.scheduled_for < deadline


def test_short_deadline_uses_compressed_reminders_not_full_cadence():
    discovered = _dt("2026-09-09T10:00:00+00:00")
    deadline = _dt("2026-09-10T09:00:00+00:00")  # ~23 hours out
    plan = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    types = [p.reminder_type for p in plan]
    assert "NEW_ASSIGNMENT" in types
    assert "T_MINUS_3D" not in types
    assert "T_MINUS_2D" not in types
    # No more than a handful of pings for a short-fuse task.
    assert len(plan) <= 4


def test_due_within_hours_gets_minimal_burst():
    discovered = _dt("2026-09-09T20:00:00+00:00")
    deadline = _dt("2026-09-09T23:00:00+00:00")  # 3 hours out
    plan = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    types = [p.reminder_type for p in plan]
    assert "NEW_ASSIGNMENT" in types
    assert len(plan) <= 3


def test_already_overdue_when_discovered_gets_no_reminders():
    discovered = _dt("2026-09-11T00:00:00+00:00")
    deadline = _dt("2026-09-10T23:59:00+00:00")
    plan = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    assert plan == []


def test_plan_is_deterministic():
    discovered = _dt("2026-09-01T09:00:00+00:00")
    deadline = _dt("2026-09-10T23:59:00+00:00")
    plan1 = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    plan2 = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    assert plan1 == plan2
