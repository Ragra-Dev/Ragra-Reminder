"""FAST timetable adapter: the ONLY place that talks to the FAST timetable
Google Spreadsheet or knows anything about its specific layout. If FAST
changes tab names, column order, or the spreadsheet ID, this is the one
file that needs to change - nothing downstream (normalization, enrollment
matching, or sync/persistence) depends on today's layout.

The spreadsheet is publicly readable (confirmed live: both opening it in a
browser with no Google account, and fetching it with a plain unauthenticated
HTTP request, work). Values are always read through the public Google
Visualization ("gviz") CSV export, which needs no credential of any kind -
not even an API key - and can address a tab by its exact title directly
(confirmed live), so no gid is ever required for reading data.

The one thing the fully public path cannot do is *enumerate* the real tab
titles - there is no public "list the tabs" endpoint. Genuine enumeration
needs the Sheets API's metadata call (spreadsheets.get), which requires
some credential even for a public file; a plain, free, read-only API key
(developerKey) is used for that - no login, no consent screen, no refresh
token, nothing tied to any Google account. The API key is OPTIONAL: without
one, weekday tabs are discovered by trying each weekday's canonical English
name against the same public endpoint and confirming the result's own
content resolves to that weekday. That works today and needs zero setup,
but is a guess-and-verify fallback, not true enumeration - it would miss a
tab renamed to something the guesses don't include (e.g. "Monday Fall
2027"). Configuring the API key upgrades to true enumeration automatically;
nothing else in the pipeline changes either way.

This deliberately does NOT reuse Ragra's Classroom or Calendar OAuth
credentials (different auth mechanism entirely, different trust boundary)
and does NOT touch Hermes in any way.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.parse
import urllib.request
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
    sheet_id: int | None  # the gid, if known from metadata enumeration - stable even if the tab is renamed


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


def _fetch_public_gviz_csv(spreadsheet_id: str, *, sheet_name: str, timeout: int = 15) -> list[list[str]] | None:
    """Fetch one tab's raw values via the fully public gviz endpoint - no
    credential of any kind. Returns None if the name doesn't correspond to
    a real tab (or the request otherwise fails), so callers can treat that
    as 'no such tab' rather than a hard error."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    return list(csv.reader(io.StringIO(raw)))


def discover_weekday_tabs_via_public_names(spreadsheet_id: str) -> dict[int, SheetInfo]:
    """Zero-credential fallback discovery, used when no API key is
    configured: try each weekday's canonical English name (a few case
    variants) against the public gviz endpoint, and only accept a hit once
    the fetched grid's own first cell independently resolves to that same
    weekday via normalize_weekday_name - so a coincidental name match on
    unrelated content can't be mistaken for the real tab. This finds today's
    real tabs correctly and needs no setup at all, but - unlike
    discover_weekday_tabs - it cannot notice a tab renamed to something
    none of the tried variants match."""
    found: dict[int, SheetInfo] = {}
    for day_index, name in enumerate(WEEKDAYS[:6]):  # Monday..Saturday
        for candidate in (name.capitalize(), name.upper(), name.lower()):
            grid = _fetch_public_gviz_csv(spreadsheet_id, sheet_name=candidate)
            if grid and grid[0] and normalize_weekday_name(grid[0][0] or "") == day_index:
                found[day_index] = SheetInfo(title=candidate, sheet_id=None)
                break
    return found


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


def _resolve_override_against_column(
    override: tuple[str, str], column_reference: tuple[str, str]
) -> tuple[str, str]:
    """Resolve an embedded per-cell time override's AM/PM using the period
    (AM/PM) already established by its column's own resolved time - a
    lone override has no sequence of its own to resolve that from, but it
    always describes a class within the same column/period."""
    column_is_pm = int(column_reference[0].split(":")[0]) >= 12

    def _adjust(value: str) -> str:
        hour_str, minute_str = value.split(":")
        hour = int(hour_str)
        if column_is_pm and hour < 12:
            hour += 12
        return f"{hour:02d}:{minute_str}"

    return _adjust(override[0]), _adjust(override[1])


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

            # Each time slot is usually a merged cell spanning several
            # columns (FAST groups a handful of columns per slot), and a
            # real course cell can land on ANY column within that span, not
            # just the one the header text itself sits on (confirmed live:
            # a header at column 21 had a real course cell at column 22, in
            # the same visual slot). So each non-blank header cell's mapping
            # applies to every column from its own index up to (but not
            # including) the next non-blank header cell - time or label -
            # rather than just its single column.
            entries = [(index, cell.strip()) for index, cell in enumerate(row) if cell and cell.strip()]
            time_index = 0
            current_room_source = 0
            for position, (index, text) in enumerate(entries):
                span_end = entries[position + 1][0] if position + 1 < len(entries) else len(row)
                if _TIME_RANGE_ONLY_PATTERN.match(text):
                    for col in range(index, span_end):
                        column_time[col] = resolved[time_index]
                        column_room_source[col] = current_room_source
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
            if parsed.time_override:
                # A cell's own embedded time text (e.g. "02:30-04:20") has
                # no left-to-right sequence of its own to resolve AM/PM
                # from - but it always describes the same class as the
                # column it sits in, so that column's already-resolved
                # period (AM/PM) is the correct context to resolve it
                # against. Confirmed live: a real cell read "02:30-04:20"
                # in a column resolved to 14:30-15:50 - the override
                # genuinely extends 30 minutes past the column's normal
                # slot (to 16:20), and is only correct once read as PM
                # like its column, not as literal 02:30-04:20 (early
                # morning) nor discarded outright.
                start_time, end_time = _resolve_override_against_column(parsed.time_override, column_time[col])
            else:
                start_time, end_time = column_time[col]
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
    """Values are always read via the fully public gviz endpoint - no
    credential of any kind. The API key (optional) is only ever used for
    the metadata call that enables true tab-title enumeration; without one,
    discover_tabs() falls back to the zero-credential name-guessing mode."""

    def __init__(self, spreadsheet_id: str, api_key: str | None = None):
        self._spreadsheet_id = spreadsheet_id
        self._api_key = api_key
        self._service = None
        if api_key:
            from googleapiclient.discovery import build

            self._service = build("sheets", "v4", developerKey=api_key, cache_discovery=False)

    @property
    def has_metadata_access(self) -> bool:
        return self._service is not None

    def list_sheets(self) -> list[SheetInfo]:
        """True enumeration via the Sheets API. Requires an API key -
        raises FastTimetableAdapterError if none was configured, rather
        than silently falling back (callers should use discover_tabs() if
        they want the automatic fallback)."""
        if self._service is None:
            raise FastTimetableAdapterError(
                "No API key configured - metadata enumeration is unavailable. "
                "Use discover_tabs() for the zero-credential fallback."
            )
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

    def discover_tabs(self) -> dict[int, SheetInfo]:
        """Best available weekday-tab discovery: true metadata enumeration
        if an API key is configured (robust to a tab being renamed to
        anything), otherwise the public name-guessing fallback (works
        today with zero setup, but only recognizes today's canonical
        weekday-name conventions - see discover_weekday_tabs_via_public_names)."""
        if self._service is not None:
            return discover_weekday_tabs(self.list_sheets())
        return discover_weekday_tabs_via_public_names(self._spreadsheet_id)

    def get_values(self, sheet_title: str) -> list[list[str]]:
        """Raw cell text for one tab, as a 2D array, via the public gviz
        endpoint - no credential required. No formatting/colors -
        deliberately not needed (see docs/PROJECT_STATUS.md investigation)."""
        grid = _fetch_public_gviz_csv(self._spreadsheet_id, sheet_name=sheet_title)
        if grid is None:
            raise FastTimetableAdapterError(f"Failed to read FAST tab {sheet_title!r} via the public endpoint.")
        return grid
