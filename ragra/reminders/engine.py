"""Deterministic reminder scheduling.

Pure functions: given a task's actual deadline and the moment it was
discovered, compute the fixed set of reminder instances that should exist.
No I/O here - ragra/reminders/dispatch.py turns this into idempotent
database rows and outbound sends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ReminderPlan:
    reminder_type: str
    scheduled_for: datetime


def compute_reminder_plan(
    *, actual_deadline: datetime, discovered_at: datetime
) -> list[ReminderPlan]:
    """Return the reminders that should exist for a task with this deadline.

    The schedule compresses as the available lead time (deadline minus the
    moment the task was discovered) shrinks, so a same-day assignment gets a
    couple of well-timed pings instead of the full multi-day cadence
    collapsed into a burst.
    """

    lead_time = actual_deadline - discovered_at
    if lead_time <= timedelta(0):
        # Already overdue when discovered; nothing meaningful to schedule.
        return []

    plan: list[ReminderPlan] = []

    if lead_time >= timedelta(days=3):
        for days, reminder_type in ((3, "T_MINUS_3D"), (2, "T_MINUS_2D"), (1, "T_MINUS_1D")):
            candidate = actual_deadline - timedelta(days=days)
            if candidate > discovered_at:
                plan.append(ReminderPlan(reminder_type, candidate))
        plan.append(ReminderPlan("DUE_TODAY", _same_day_morning(actual_deadline)))
        plan.append(ReminderPlan("FINAL_1H", actual_deadline - timedelta(hours=1)))

    elif lead_time >= timedelta(days=1):
        # Posted 1-3 days out: compressed cadence, no multi-day countdown.
        plan.append(ReminderPlan("NEW_ASSIGNMENT", discovered_at))
        one_day_before = actual_deadline - timedelta(days=1)
        if one_day_before > discovered_at:
            plan.append(ReminderPlan("T_MINUS_1D", one_day_before))
        plan.append(ReminderPlan("FEW_HOURS", actual_deadline - timedelta(hours=3)))
        plan.append(ReminderPlan("FINAL_1H", actual_deadline - timedelta(hours=1)))

    else:
        # Due within a day of discovery: short, spam-free burst.
        plan.append(ReminderPlan("NEW_ASSIGNMENT", discovered_at))
        midpoint = discovered_at + (lead_time / 2)
        if midpoint > discovered_at + timedelta(minutes=5):
            plan.append(ReminderPlan("MIDPOINT", midpoint))
        if lead_time > timedelta(hours=1):
            plan.append(ReminderPlan("FINAL_1H", actual_deadline - timedelta(hours=1)))

    # Only keep reminders that are still in the future relative to discovery,
    # and that fire strictly before the deadline itself.
    return [r for r in plan if discovered_at <= r.scheduled_for < actual_deadline]


def _same_day_morning(deadline: datetime) -> datetime:
    return deadline.replace(hour=8, minute=0, second=0, microsecond=0)


def reminder_message(reminder_type: str, task_title: str, course_code: str | None) -> str:
    label = f"{course_code}: {task_title}" if course_code else task_title
    messages = {
        "NEW_ASSIGNMENT": f"New assignment: {label}",
        "T_MINUS_3D": f"Due in 3 days: {label}",
        "T_MINUS_2D": f"Due in 2 days: {label}",
        "T_MINUS_1D": f"Due tomorrow: {label}",
        "DUE_TODAY": f"Due today: {label}",
        "FEW_HOURS": f"Reminder: {label} due soon",
        "MIDPOINT": f"Reminder: {label}",
        "FINAL_1H": f"⚠️ {label} due in 1 hour",
    }
    return messages.get(reminder_type, label)
