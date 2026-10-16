from datetime import datetime, timedelta, timezone

from ragra.reminders.engine import compute_reminder_plan


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_a_long_lead_assignment_is_announced_when_discovered():
    """A 16-day-out assignment must still say "something new was posted".

    This is the case that failed in real use: the countdown ladder was
    scheduled correctly but the discovery ping was omitted for any lead
    time of 3 days or more, so a task posted two weeks ahead produced
    complete silence until T_MINUS_7D - nine days later.
    """
    discovered = _dt("2026-09-04T11:57:57+00:00")
    deadline = _dt("2026-09-20T18:59:00+00:00")
    plan = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    types = [p.reminder_type for p in plan]

    assert types.count("NEW_ASSIGNMENT") == 1
    # ...and it fires immediately, not at some point in the countdown.
    announcement = next(p for p in plan if p.reminder_type == "NEW_ASSIGNMENT")
    assert announcement.scheduled_for == discovered
    # The countdown ladder is unchanged.
    for expected in ("T_MINUS_7D", "T_MINUS_5D", "T_MINUS_2D", "DUE_TODAY", "FINAL_1H"):
        assert expected in types


def test_every_branch_emits_exactly_one_discovery_ping():
    """The three lead-time branches must agree on this. The defect was an
    asymmetry between them, so the property is asserted across all three
    rather than in whichever branch happens to be edited."""
    discovered = _dt("2026-09-01T09:00:00+00:00")
    for label, deadline in (
        ("long lead", _dt("2026-09-20T18:00:00+00:00")),
        ("medium lead", _dt("2026-09-03T09:00:00+00:00")),
        ("short lead", _dt("2026-09-01T15:00:00+00:00")),
    ):
        plan = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
        types = [p.reminder_type for p in plan]
        assert types.count("NEW_ASSIGNMENT") == 1, f"{label} branch"


def test_normal_task_gets_full_countdown():
    discovered = _dt("2026-09-01T09:00:00+00:00")
    deadline = _dt("2026-09-10T23:59:00+00:00")
    plan = compute_reminder_plan(actual_deadline=deadline, discovered_at=discovered)
    types = [p.reminder_type for p in plan]
    assert "NEW_ASSIGNMENT" in types
    assert "T_MINUS_7D" in types
    assert "T_MINUS_5D" in types
    assert "T_MINUS_2D" in types
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
    assert "T_MINUS_7D" not in types
    assert "T_MINUS_5D" not in types
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
