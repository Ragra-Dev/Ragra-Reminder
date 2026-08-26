import pytest

from ragra.adapters.fast_timetable import AmbiguousTimetableStructureError, FastTimetableAdapterError, SheetInfo
from ragra.db import repo
from ragra.sync.timetable_sync import TimetableSyncError, sync_timetable
from ragra.timetable.enrollment import REGULAR, REPEAT, EnrolledCourse

ENROLLMENT = (
    EnrolledCourse("Linear Algebra", "CS-G", REGULAR, batch_year="2025", aliases=("LA",)),
    EnrolledCourse("DLD", "CS-G", REGULAR, batch_year="2025"),
    EnrolledCourse("DLD Lab", "CS-G", REGULAR, batch_year="2025"),
    EnrolledCourse("UHQ-I&II", "CS-G", REGULAR, batch_year="2025"),
    EnrolledCourse("Discrete Structures", "CS-B", REPEAT, aliases=("Discrete",)),
    EnrolledCourse("OOP Theory", "CS-C", REPEAT, aliases=("OOP",)),
    EnrolledCourse("OOP Lab", "CS-A", REPEAT),
)


class FakeFastTimetableClient:
    """Duck-types FastTimetableClient. Grids are per-tab-title, fully
    controlled by the test - no network involved."""

    def __init__(self, sheets: list[SheetInfo], grids: dict[str, list[list[str]]]):
        self._sheets = sheets
        self._grids = grids

    def list_sheets(self):
        return self._sheets

    def get_values(self, sheet_title: str):
        return self._grids[sheet_title]


MONDAY_GRID = [
    ["Monday", "", ""],
    ["Room/ Time", "13:00-14:20", "14:30-16:20"],
    ["C-311", "LA (CS-G)", "UHQ-I&II (CS-G)"],
    ["Lab", "08:30-11:15"],
    ["B-Digital", "DLD Lab (CS-G)"],
]

TUESDAY_GRID = [
    ["Tuesday", "", "", ""],
    ["Room/ Time", "08:30-09:50", "14:30-15:50", "15:55-17:15"],
    ["C-308", "DLD (CS-G)", "", ""],
    ["C-307", "", "OOP (CS-C, 25)", "Discrete (CS-B, 25)"],
]

FRIDAY_GRID = [
    ["Friday", ""],
    ["Lab", "14:30-17:15"],
    ["Margala 1 (C-209)", "OOP Lab (CS-A)"],
]

BASE_SHEETS = [
    SheetInfo(title="WELCOME", sheet_id=1174567785),
    SheetInfo(title="MONDAY", sheet_id=1882612924),
    SheetInfo(title="TUESDAY", sheet_id=945396749),
    SheetInfo(title="FRIDAY", sheet_id=1783333514),
]

BASE_GRIDS = {"MONDAY": MONDAY_GRID, "TUESDAY": TUESDAY_GRID, "FRIDAY": FRIDAY_GRID}


def _sync(conn, sheets=None, grids=None, enrollment=ENROLLMENT):
    client = FakeFastTimetableClient(sheets or BASE_SHEETS, grids or BASE_GRIDS)
    return sync_timetable(conn, client, spreadsheet_id="test-sheet-id", enrollment=enrollment)


def test_regular_enrollment_classes_are_synced(conn):
    summary = _sync(conn)
    assert summary.classes_created >= 3  # LA, UHQ, DLD Lab at minimum
    events = {row["course_name"]: row for row in repo.list_timetable_events(conn)}
    assert events["Linear Algebra"]["section"] == "CS-G"
    assert events["Linear Algebra"]["enrollment_type"] == REGULAR
    assert events["Linear Algebra"]["batch_year"] == "2025"


def test_repeat_enrollment_classes_are_synced_without_batch_year(conn):
    _sync(conn)
    events = {row["course_name"]: row for row in repo.list_timetable_events(conn)}
    assert events["OOP Theory"]["enrollment_type"] == REPEAT
    assert events["OOP Theory"]["batch_year"] is None
    assert events["Discrete Structures"]["enrollment_type"] == REPEAT


def test_repeat_theory_and_repeat_lab_persist_with_different_sections(conn):
    _sync(conn)
    events = {row["course_name"]: row for row in repo.list_timetable_events(conn)}
    assert events["OOP Theory"]["section"] == "CS-C"
    assert events["OOP Lab"]["section"] == "CS-A"
    assert events["OOP Theory"]["section"] != events["OOP Lab"]["section"]


def test_repeated_sync_is_idempotent_no_duplicates(conn):
    summary1 = _sync(conn)
    summary2 = _sync(conn)
    summary3 = _sync(conn)

    assert summary1.classes_created > 0
    assert summary2.classes_created == 0
    assert summary3.classes_created == 0
    assert summary2.classes_unchanged == summary1.classes_found

    total_rows = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert total_rows == summary1.classes_found


def test_time_and_room_change_updates_existing_row_not_a_duplicate(conn):
    _sync(conn)
    before = {row["course_name"]: row["id"] for row in repo.list_timetable_events(conn)}

    changed_tuesday = [row[:] for row in TUESDAY_GRID]
    changed_tuesday[2] = ["C-999", "DLD (CS-G)", "", ""]  # room changed from C-308 to C-999
    summary = _sync(conn, grids={**BASE_GRIDS, "TUESDAY": changed_tuesday})

    after = {row["course_name"]: row for row in repo.list_timetable_events(conn)}
    assert after["DLD"]["id"] == before["DLD"]  # same logical row, not a new one
    assert after["DLD"]["room"] == "C-999"
    assert summary.classes_updated >= 1

    total_rows = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert total_rows == len(before)  # no duplicate created


def test_day_change_is_detected_as_a_modification_not_a_new_plus_removed_class(conn):
    _sync(conn)
    before_id = {row["course_name"]: row["id"] for row in repo.list_timetable_events(conn)}["Discrete Structures"]

    # Move Discrete Structures from Tuesday to Wednesday, same section.
    moved_grid = [
        ["Wednesday", "", "", ""],
        ["Room/ Time", "08:30-09:50", "14:30-15:50", "15:55-17:15"],
        ["C-308", "DLD (CS-G)", "", ""],
        ["C-307", "", "OOP (CS-C, 25)", "Discrete (CS-B, 25)"],
    ]
    sheets = BASE_SHEETS + [SheetInfo(title="WEDNESDAY", sheet_id=542677125)]
    grids = {**BASE_GRIDS, "TUESDAY": [["Tuesday", "", "", ""]], "WEDNESDAY": moved_grid}
    _sync(conn, sheets=sheets, grids=grids)

    after = {row["course_name"]: row for row in repo.list_timetable_events(conn)}
    assert after["Discrete Structures"]["id"] == before_id
    assert after["Discrete Structures"]["day_of_week"] == 2  # Wednesday


def test_cancelled_cell_marks_existing_event_cancelled(conn):
    _sync(conn)
    cancelled_tuesday = [row[:] for row in TUESDAY_GRID]
    cancelled_tuesday[2] = ["C-308", "DLD (CS-G) Cancelled", "", ""]
    _sync(conn, grids={**BASE_GRIDS, "TUESDAY": cancelled_tuesday})

    events = {row["course_name"]: row for row in repo.list_timetable_events(conn)}
    assert events["DLD"]["status"] == "CANCELLED"


def test_missing_weekday_tabs_raises_and_preserves_existing_data(conn):
    _sync(conn)
    before_count = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert before_count > 0

    class BrokenClient:
        def list_sheets(self):
            return [SheetInfo(title="WELCOME", sheet_id=1)]  # no weekday tabs at all

        def get_values(self, sheet_title):
            raise AssertionError("should never be called - discovery must fail first")

    with pytest.raises(TimetableSyncError):
        sync_timetable(conn, BrokenClient(), spreadsheet_id="test-sheet-id", enrollment=ENROLLMENT)

    after_count = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert after_count == before_count  # untouched, not wiped


def test_ambiguous_weekday_tabs_raises_and_preserves_existing_data(conn):
    _sync(conn)
    before_count = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]

    class AmbiguousClient:
        def list_sheets(self):
            return [SheetInfo(title="Monday A", sheet_id=1), SheetInfo(title="Monday B", sheet_id=2)]

        def get_values(self, sheet_title):
            raise AssertionError("should never be called - discovery must fail first")

    with pytest.raises(TimetableSyncError):
        sync_timetable(conn, AmbiguousClient(), spreadsheet_id="test-sheet-id", enrollment=ENROLLMENT)

    after_count = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert after_count == before_count


def test_adapter_failure_mid_scrape_preserves_existing_data(conn):
    _sync(conn)
    before_count = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]

    class FlakyClient:
        def list_sheets(self):
            return BASE_SHEETS

        def get_values(self, sheet_title):
            raise FastTimetableAdapterError("simulated network failure")

    with pytest.raises(TimetableSyncError):
        sync_timetable(conn, FlakyClient(), spreadsheet_id="test-sheet-id", enrollment=ENROLLMENT)

    after_count = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert after_count == before_count


def test_someone_elses_section_is_not_synced(conn):
    tuesday_with_other_section = [row[:] for row in TUESDAY_GRID]
    tuesday_with_other_section.append(["C-310", "DLD (CS-A)", "", ""])  # not the user's section
    _sync(conn, grids={**BASE_GRIDS, "TUESDAY": tuesday_with_other_section})

    events = repo.list_timetable_events(conn)
    assert all(row["section"] != "CS-A" for row in events if row["course_name"] == "DLD")
