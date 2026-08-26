"""Pure text normalization for FAST timetable data. No network access here -
this module only ever operates on strings already retrieved by
ragra/adapters/fast_timetable.py, so it can be unit-tested against fixtures
with zero mocking of network calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WEEKDAYS: tuple[str, ...] = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def normalize_weekday_name(raw: str) -> int | None:
    """Return 0=Monday..6=Sunday for a tab title that merely *contains* a
    weekday name, tolerant of extra words/years FAST might append (e.g.
    "Monday Fall 2026", "Mon", "MONDAY"). Returns None if no weekday name is
    recognizable, rather than guessing."""
    text = raw.strip().lower()
    for index, name in enumerate(WEEKDAYS):
        if name in text or text.startswith(name[:3]):
            return index
    return None


def normalize_section(value: str) -> str:
    """Whitespace/case-insensitive canonical form of a section string, e.g.
    "cs g", "CS_G", "CS-G" all normalize to "CS-G". Purely textual - this
    does not assign any regular/repeat meaning to the result."""
    text = value.strip().upper().replace("_", "-")
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text


def normalize_course_text(value: str) -> str:
    """Case/whitespace-insensitive canonical form for course-name matching."""
    return re.sub(r"\s+", " ", value.strip().lower())


_CELL_PATTERN = re.compile(
    r"^(?P<course>.+?)\s*"
    r"\(\s*(?P<section>[^,()]+?)\s*(?:,\s*(?P<year>\d{2,4})\s*)?\)"
    r"(?P<rest>.*)$"
)

_TIME_OVERRIDE_PATTERN = re.compile(r"(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})")
_CANCELLED_PATTERN = re.compile(r"cancel+ed", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedCell:
    course_text: str
    section: str
    year_suffix: str | None
    time_override: tuple[str, str] | None
    cancelled: bool


def parse_course_cell(raw: str) -> ParsedCell | None:
    """Parse a single non-empty timetable cell such as "OOP (CS-C, 25)",
    "DLD (CS-G)", or "Ideology of Pak (CS-F) 01:00-02:45" into its parts.
    Returns None for a cell that doesn't match the "name (section[, year])"
    shape at all (e.g. a bare room label or a blank continuation cell) -
    callers must treat that as "nothing extractable here", never guess."""
    text = raw.strip()
    if not text:
        return None
    match = _CELL_PATTERN.match(text)
    if not match:
        return None

    rest = match.group("rest") or ""
    time_match = _TIME_OVERRIDE_PATTERN.search(rest)
    time_override = (time_match.group("start"), time_match.group("end")) if time_match else None

    return ParsedCell(
        course_text=match.group("course").strip(),
        section=normalize_section(match.group("section")),
        year_suffix=match.group("year"),
        time_override=time_override,
        cancelled=bool(_CANCELLED_PATTERN.search(rest)),
    )


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def resolve_24h_time_sequence(raw_slots: list[str]) -> list[tuple[str, str]]:
    """FAST publishes each day's time-slot headers left-to-right in natural
    chronological order (e.g. "08:30-09:50", ..., "11:30-12:50",
    "01:00-02:20", ...) but without AM/PM markers once the day crosses
    noon - and a single slot can itself span the crossing (e.g. a long lab
    block "11:30-02:15"). Resolve this the way a human reading the sequence
    left-to-right would: walk every hour value (each slot's start, then its
    end, in order) and flip into "afternoon" the moment a value is smaller
    than the one immediately before it - from then on, every subsequent
    value gets +12 (except a literal 12, which is already correct in either
    half of the day). This only assumes the slots are listed in real
    chronological order; it never hardcodes a specific crossover time or a
    fixed number of slots, so it keeps working if FAST adds, removes, or
    resizes slots."""
    parsed_slots = [_parse_hhmm_pair(slot) for slot in raw_slots]

    pm = False
    previous_hour: int | None = None
    resolved_hours: list[int] = []
    for start_h, _start_m, end_h, _end_m in parsed_slots:
        for hour in (start_h, end_h):
            if previous_hour is not None and hour < previous_hour:
                pm = True
            previous_hour = hour
            resolved_hours.append(hour + 12 if (pm and hour != 12) else hour)

    resolved: list[tuple[str, str]] = []
    for index, (_start_h, start_m, _end_h, end_m) in enumerate(parsed_slots):
        start_24 = resolved_hours[index * 2]
        end_24 = resolved_hours[index * 2 + 1]
        resolved.append((f"{start_24:02d}:{start_m:02d}", f"{end_24:02d}:{end_m:02d}"))

    return resolved


def _parse_hhmm_pair(slot: str) -> tuple[int, int, int, int]:
    start_raw, end_raw = slot.split("-")
    start_h, start_m = _parse_hhmm(start_raw.strip())
    end_h, end_m = _parse_hhmm(end_raw.strip())
    return start_h, start_m, end_h, end_m
