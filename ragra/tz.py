"""Campus-local time handling - the single boundary between wall-clock and
instants.

Ragra stores every instant as UTC (see ragra/db/repo.py's now_iso) and
compares instants lexicographically in SQL. Wall-clock values from the FAST
timetable are a different kind of thing entirely: they are campus-local
recurrence patterns ("08:30 on Mondays"), not instants. When the timetable
says 08:30, the university means 08:30 *on campus*, regardless of what any
offset table says - so the wall-clock value is authoritative and its UTC
projection is derived, never the other way round.

This module owns that one conversion. Nothing timezone-derived is ever
persisted as a future instant (class occurrences are computed on demand -
see ragra/timetable/schedule.py), so a timezone-database update can never
leave a stored row silently wrong.

The zone is resolved by IANA name, never as a fixed offset. Pakistan
currently observes no DST and sits at UTC+05:00, but that is a political
fact, not a physical one - DST was observed in 2002, 2008 and 2009 - so a
hardcoded +05:00 would bake a government policy decision into the codebase
as if it were a constant. Missing timezone data raises loudly rather than
falling back to a guessed offset, because a silent fallback is exactly the
failure this module exists to prevent.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Karachi"


class TimezoneDataUnavailable(RuntimeError):
    """The IANA timezone database could not be loaded. Deliberately fatal:
    guessing a fixed offset instead would silently produce wrong class
    times and wrong reminder instants."""


def timezone_name() -> str:
    return os.environ.get("RAGRA_TIMEZONE") or DEFAULT_TIMEZONE


def campus_zone(name: str | None = None) -> ZoneInfo:
    """Resolve the campus timezone by IANA name. Raises
    TimezoneDataUnavailable (never falls back to a fixed offset) if the
    timezone database is missing - on Windows this means the `tzdata`
    package is not installed."""
    resolved = name or timezone_name()
    try:
        return ZoneInfo(resolved)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneDataUnavailable(
            f"could not load timezone {resolved!r}: {exc}. Ragra will not guess a "
            f"fixed UTC offset, because that silently produces wrong class times "
            f"whenever the zone's rules differ. Install the 'tzdata' package."
        ) from exc


def require_aware(value: datetime, *, what: str = "datetime") -> datetime:
    """Naive datetimes must never cross a module boundary - a naive value
    here means some caller lost the timezone, and every downstream
    comparison would be quietly wrong."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got naive {value!r}")
    return value


def to_utc(value: datetime) -> datetime:
    return require_aware(value, what="instant").astimezone(timezone.utc)


def to_local(value: datetime, *, zone: ZoneInfo | None = None) -> datetime:
    return require_aware(value, what="instant").astimezone(zone or campus_zone())


def utc_iso(value: datetime) -> str:
    """The one canonical stored form for a UTC instant: ISO 8601 with an
    explicit +00:00 offset, matching everything Ragra already writes. Never
    'Z' - mixing the two breaks the lexicographic ordering the SQL layer
    depends on ('+' sorts before 'Z')."""
    return to_utc(value).isoformat()


def parse_instant(value: str) -> datetime:
    """Parse a stored timestamp into an aware UTC datetime. Accepts both the
    '+00:00' form Ragra writes and the 'Z' form Google Classroom returns, so
    callers can compare the two safely as instants instead of as text."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"stored timestamp {value!r} has no timezone offset")
    return parsed.astimezone(timezone.utc)


def _offset_before(value: datetime):
    return value.replace(fold=0).utcoffset()


def _offset_after(value: datetime):
    return value.replace(fold=1).utcoffset()


def is_ambiguous(value: datetime) -> bool:
    """True for a wall-clock time that happens twice (fall-back overlap)."""
    return _offset_before(value) != _offset_after(value) and not is_nonexistent(value)


def is_nonexistent(value: datetime) -> bool:
    """True for a wall-clock time that never happens (spring-forward gap).
    Such a value does not survive a round trip through UTC."""
    return value.astimezone(timezone.utc).astimezone(value.tzinfo) != value


def combine_local(day: date, time_of_day: time, *, zone: ZoneInfo | None = None) -> datetime:
    """Build the aware local datetime for a campus wall-clock slot.

    Two transition cases are handled explicitly. Pakistan has no DST today,
    so neither can currently occur there - but the rules can change, and
    code that silently does the wrong thing at a transition is exactly the
    class of bug this design exists to prevent. Both are therefore tested
    against a DST-observing zone, not against Asia/Karachi (which would
    leave the branches unexercised and the tests vacuously green).

    - Nonexistent (spring forward): shifted forward by the size of the gap,
      landing on the first real instant after the transition.
    - Ambiguous (fall back): the first (earlier) of the two occurrences,
      i.e. fold=0. Earlier is the safe direction for a class reminder.
    """
    resolved = zone or campus_zone()
    local = datetime.combine(day, time_of_day).replace(tzinfo=resolved)
    if is_nonexistent(local):
        gap = _offset_after(local) - _offset_before(local)
        local = local + gap
    return local.replace(fold=0)


def local_day_bounds(
    instant: datetime, *, zone: ZoneInfo | None = None
) -> tuple[datetime, datetime]:
    """Inclusive UTC bounds of the *local* calendar day containing `instant`.

    This is what makes "today" mean the campus calendar day rather than the
    UTC one. Each midnight is computed independently via combine_local so
    the bounds stay correct across a DST transition, instead of assuming
    every day is exactly 24 hours long.
    """
    resolved = zone or campus_zone()
    local_day = to_local(instant, zone=resolved).date()
    start = combine_local(local_day, time(0, 0), zone=resolved)
    next_start = combine_local(local_day + timedelta(days=1), time(0, 0), zone=resolved)
    end = next_start - timedelta(microseconds=1)
    return to_utc(start), to_utc(end)


def format_local(value: datetime, *, zone: ZoneInfo | None = None) -> str:
    """Display form, always zone-labelled. The label matters: an unlabelled
    time that is silently in the wrong zone looks perfectly plausible to a
    reader, which is how a wrong conversion survives review."""
    local = to_local(value, zone=zone)
    label = local.strftime("%Z") or local.strftime("%z")
    return f"{local.strftime('%a %d %b %Y, %I:%M %p').replace(' 0', ' ')} {label}".strip()


def format_stored_local(value: str | None, *, zone: ZoneInfo | None = None) -> str:
    """Same as format_local for a stored ISO timestamp. Returns a dash for a
    missing value rather than inventing one."""
    if not value:
        return "—"
    try:
        return format_local(parse_instant(value), zone=zone)
    except ValueError:
        # A stored value Ragra can't parse (e.g. a legacy date-only
        # personal deadline) is shown verbatim rather than guessed at.
        return value
