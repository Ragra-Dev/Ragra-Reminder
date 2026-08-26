"""Tests for extract_classes_from_grid against fixture grids modeled on the
real FAST spreadsheet's structure (confirmed by direct inspection of the
live sheet): two side-by-side room/time-slot blocks sharing row numbers,
and a separate lower "Lab" header with its own, longer time slots. These
fixtures are constructed to match that confirmed structure, not literal
verbatim historical snapshots of every cell.
"""

from ragra.adapters.fast_timetable import extract_classes_from_grid

# A condensed but structurally real two-block lecture grid (Tuesday-shaped):
# block 1 = C-building rooms/columns 1-2 (BS CS), block 2 = D-building
# rooms/columns 4-5 (BS Repeat/other), matching the real sheet's pattern of
# a second "Room" label column resetting the room source further right.
#            col0            col1            col2  col3     col4
LECTURE_GRID = [
    ["Tuesday", "", "", "", ""],
    ["Room/ Time", "08:30-09:50", "", "Room", "10:00-11:20"],
    ["C-308", "DLD (CS-G)", "", "D-306", ""],
    ["C-307", "", "", "D-307", "OOP (CS-C, 25)"],
]

# A condensed but structurally real lab-block grid, with its own separate
# header (longer slots, different times) appearing after the lecture block.
LAB_GRID = [
    ["Lab", "08:30-11:15", "11:30-02:15"],
    ["Margala 1 (C-209)", "OOP Lab (CS-A)", ""],
    ["B-Digital", "DLD Lab (CS-G)", ""],
]


def test_extracts_regular_course_with_room_and_resolved_time():
    result = extract_classes_from_grid(LECTURE_GRID, day_of_week=1)
    dld = next(c for c in result if c.course_text == "DLD")
    assert dld.section == "CS-G"
    assert dld.room == "C-308"
    assert dld.start_time == "08:30"
    assert dld.end_time == "09:50"


def test_extracts_repeat_course_from_second_block_with_its_own_room_column():
    result = extract_classes_from_grid(LECTURE_GRID, day_of_week=1)
    oop = next(c for c in result if c.course_text == "OOP")
    assert oop.section == "CS-C"
    assert oop.year_suffix == "25"
    # Room must come from the SECOND block's own room column (D-307), not
    # be incorrectly inherited from the first block's C-307/C-308 rooms.
    assert oop.room == "D-307"
    assert oop.start_time == "10:00"
    assert oop.end_time == "11:20"


def test_extracts_lab_entries_with_lab_specific_time_header():
    result = extract_classes_from_grid(LAB_GRID, day_of_week=4)
    oop_lab = next(c for c in result if c.course_text == "OOP Lab")
    assert oop_lab.section == "CS-A"
    assert oop_lab.room == "Margala 1 (C-209)"
    assert oop_lab.start_time == "08:30"
    assert oop_lab.end_time == "11:15"

    dld_lab = next(c for c in result if c.course_text == "DLD Lab")
    assert dld_lab.section == "CS-G"
    assert dld_lab.room == "B-Digital"


def test_blank_cells_produce_no_entries():
    result = extract_classes_from_grid(LECTURE_GRID, day_of_week=1)
    # Row "C-308" has a blank in column2 (OOP's column) - must not produce a
    # phantom entry there.
    assert not any(c.room == "C-308" and c.course_text != "DLD" for c in result)
