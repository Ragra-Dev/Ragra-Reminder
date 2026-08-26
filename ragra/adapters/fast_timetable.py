"""FAST timetable adapter: the ONLY place that talks to the FAST timetable
Google Spreadsheet or knows anything about its specific layout. If FAST
changes tab names, column order, or the spreadsheet ID, this is the one
file that needs to change - nothing downstream (normalization, enrollment
matching, or sync/persistence) depends on today's layout.

The spreadsheet is publicly readable (confirmed: opens in an incognito
window with no Google account). The Sheets API still requires some
credential on every call regardless of the target file's sharing settings,
so this uses a plain, free, read-only API key (developerKey) rather than
OAuth - no login, no consent screen, no refresh token, nothing tied to any
Google account. This deliberately does NOT reuse Ragra's Classroom or
Calendar OAuth credentials (different auth mechanism entirely, different
trust boundary) and does NOT touch Hermes in any way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ragra.timetable.normalize import WEEKDAYS, normalize_weekday_name, parse_course_cell, resolve_24h_time_sequence

_TIME_RANGE_ONLY_PATTERN = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")


class FastTimetableAdapterError(RuntimeError):
    """Raised when the FAST timetable source cannot be read at all (missing
    API key, network/API failure, or the metadata call itself fails)."""


class AmbiguousTimetableStructureError(RuntimeError):
    """Raised when weekday tabs can't be discovered unambiguously - e.g. a
    weekday name matches zero or multiple tabs. Callers must treat this as
    'stop and report', never as 'guess which tab is Monday'."""


@dataclass(frozen=True)
class SheetInfo:
    title: str
    sheet_id: int  # the gid - stable even if the tab is renamed


def discover_weekday_tabs(sheets: list[SheetInfo]) -> dict[int, SheetInfo]:
    """Map 0=Monday..6=Sunday to the matching tab, purely from tab titles.
    Never depends on gid values or today's exact tab names. Raises
    AmbiguousTimetableStructureError rather than guessing if a weekday
    matches zero or more than one tab."""
    by_weekday: dict[int, list[SheetInfo]] = {}
    for sheet in sheets:
        weekday = normalize_weekday_name(sheet.title)
        if weekday is not None:
            by_weekday.setdefault(weekday, []).append(sheet)

    ambiguous = {WEEKDAYS[day]: [s.title for s in matches] for day, matches in by_weekday.items() if len(matches) > 1}
    if ambiguous:
        raise AmbiguousTimetableStructureError(f"Multiple tabs match the same weekday: {ambiguous}")

    return {day: matches[0] for day, matches in by_weekday.items()}


@dataclass(frozen=True)
class ExtractedClass:
    """One raw class meeting extracted from a timetable tab, matched against
    nothing yet - enrollment matching is a separate stage (see
    ragra/timetable/match.py), never done here."""

    course_text: str
    section: str
    year_suffix: str | None
    day_of_week: int
    start_time: str
    end_time: str
    room: str | None
    cancelled: bool


def extract_classes_from_grid(grid: list[list[str]], *, day_of_week: int) -> list[ExtractedClass]:
    """Parse one weekday tab's raw cell grid into ExtractedClass entries,
    with no assumption about a fixed row/column layout.

    FAST's real sheets place more than one side-by-side room/time-slot
    block on the same rows (e.g. one block of rooms for one set of
    programs, then another "Room" column and its own time-slot header
    further right for a different set of rooms), and separately place a
    second header further down the sheet for lab slots (longer blocks,
    different times). This is handled generically: any row containing at
    least one cell that is *itself* just a bare time range (e.g.
    "08:30-09:50", nothing else in the cell) is treated as a new time-slot
    header - it resets which columns carry a time slot and what that slot
    resolves to, from that row down, until the next such header row
    appears. A real course cell always has a "Name (Section...)" shape and
    can never itself be a bare time range, so this never mistakes a data
    row for a header, even when a day has only a single time slot. A
    header cell that is non-blank but is
    NOT a bare time range (e.g. "Room", "Room/ Time") marks where a new
    "room" source column begins, so every following time-slot column reads
    its room from whichever such label most recently preceded it - this is
    what makes multiple side-by-side blocks work without hardcoding column
    0 or any fixed column index.
    """
    column_time: dict[int, tuple[str, str]] = {}
    column_room_source: dict[int, int] = {}

    extracted: list[ExtractedClass] = []

    for row in grid:
        time_cells = [
            (index, cell) for index, cell in enumerate(row) if cell and _TIME_RANGE_ONLY_PATTERN.match(cell.strip())
        ]
        # A row with at least one bare time-range cell (nothing else in that
        # cell) is a time-slot header row. A single slot is a real case
        # (e.g. a day with only one lab block), not just the common
        # multi-slot case, so this deliberately doesn't require >= 2 - a
        # real course cell always has a "Name (Section...)" shape and can
        # never itself be a bare time range, so this doesn't risk mistaking
        # a data row for a header.
        if time_cells:
            resolved = resolve_24h_time_sequence([cell for _, cell in time_cells])
            # Single left-to-right pass: whichever non-time label appeared
            # most recently is the room source for every time column that
            # follows it, until the next label. This is what lets multiple
            # side-by-side room/time blocks on the same rows resolve
            # correctly, without assuming column 0 is always "the" room.
            current_room_source = 0
            time_index = 0
            for index, cell in enumerate(row):
                text = cell.strip() if cell else ""
                if not text:
                    continue
                if _TIME_RANGE_ONLY_PATTERN.match(text):
                    column_time[index] = resolved[time_index]
                    column_room_source[index] = current_room_source
                    time_index += 1
                else:
                    current_room_source = index
            continue

        for col, cell in enumerate(row):
            if col not in column_time or not cell or not cell.strip():
                continue
            room_col = column_room_source.get(col, 0)
            room = row[room_col].strip() if room_col < len(row) and row[room_col].strip() else None
            parsed = parse_course_cell(cell)
            if parsed is None:
                continue
            start_time, end_time = parsed.time_override or column_time[col]
            extracted.append(
                ExtractedClass(
                    course_text=parsed.course_text,
                    section=parsed.section,
                    year_suffix=parsed.year_suffix,
                    day_of_week=day_of_week,
                    start_time=start_time,
                    end_time=end_time,
                    room=room,
                    cancelled=parsed.cancelled,
                )
            )

    return extracted


class FastTimetableClient:
    """The only place that touches googleapiclient for the Sheets API."""

    def __init__(self, spreadsheet_id: str, api_key: str):
        from googleapiclient.discovery import build

        self._spreadsheet_id = spreadsheet_id
        self._service = build("sheets", "v4", developerKey=api_key, cache_discovery=False)

    def list_sheets(self) -> list[SheetInfo]:
        try:
            result: dict[str, Any] = (
                self._service.spreadsheets()
                .get(spreadsheetId=self._spreadsheet_id, fields="sheets.properties(sheetId,title)")
                .execute()
            )
        except Exception as exc:
            raise FastTimetableAdapterError(f"Failed to read FAST spreadsheet metadata: {exc}") from exc

        return [
            SheetInfo(title=sheet["properties"]["title"], sheet_id=sheet["properties"]["sheetId"])
            for sheet in result.get("sheets", [])
        ]

    def get_values(self, sheet_title: str) -> list[list[str]]:
        """Raw cell text for one tab, as a 2D array. No formatting/colors -
        deliberately not needed (see docs/PROJECT_STATUS.md investigation)."""
        quoted_title = sheet_title.replace("'", "''")
        try:
            result: dict[str, Any] = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._spreadsheet_id, range=f"'{quoted_title}'")
                .execute()
            )
        except Exception as exc:
            raise FastTimetableAdapterError(f"Failed to read FAST tab {sheet_title!r}: {exc}") from exc

        return result.get("values", [])
