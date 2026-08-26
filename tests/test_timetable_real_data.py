"""Regression test against real, verbatim data captured directly from the
live FAST timetable spreadsheet (Tuesday tab, gid=945396749) via its public
gviz CSV export - not a hand-constructed fixture. This is the concrete
proof that the extraction + enrollment-matching pipeline works against the
actual spreadsheet's real structure (two side-by-side room/time blocks,
the literal "BS Repeat Courses" column-group, and the real cell text
conventions), not just idealized test data.
"""

import csv
import io

from ragra.adapters.fast_timetable import extract_classes_from_grid
from ragra.timetable.enrollment import MY_ENROLLMENT
from ragra.timetable.match import match_cell
from ragra.timetable.normalize import ParsedCell

# Verbatim, captured from:
# https://docs.google.com/spreadsheets/d/1vlTuotLw34fedME3gNQj09cZw-todVomxAiu5P1wZ6Q
#   /gviz/tq?tqx=out:csv&gid=945396749
REAL_TUESDAY_CSV = r'''"Tuesday","","","","","","BS CS (2026)","","","","","BS DS (2026)","","","","","BS AI (2026)","","","","","BS CY (2026)","","","","","BS SE (2026)","","","","","BS Repeat Courses","","","MS (CS)","","","MS (AI)",""
"","","","","","","BS CS (2025)","","","","","BS DS (2025)","","","","","BS AI (2025)","","","","","BS CY (2025)","","","","","BS SE (2025)","","","","","MS Electives (All Prgrms)","","","MS (DS)","","","MS (CY)",""
"","","","","","","BS CS (2024)","","","","","BS DS (2024)","","","","","BS AI (2024)","","","","","BS CY (2024)","","","","","BS SE (2024)","","","","","MS Computational Intelligence","","","MS AI in Health Sciences","","","MS (SE)",""
"","","","","","","BS CS (2023)","","","","","BS DS (2023)","","","","","BS AI (2023)","","","","","BS CY (2023)","","","","","BS SE (2023)","","","","","PhD (Computing)","","","","","","",""
"Room/ Time","08:30-09:50","","","","","10:00-11:20","","","","","11:30-12:50","","","","","01:00-02:20","","","","","02:30-03:50","","","","","03:55-05:15","","","","Room","05:20-06:40 ","","","","","06:45-08:05",""
"C-308","DLD (CS-G)","","","","","Data St (CS-G)","","","","","LA (CS-E)","","","","","SDA (CS-G)","","","","","","","","","","Discrete (CS-B, 25)","","","","D-311","DB & OS (CI)","","","","","Math for CI (CI) Room D-306",""
"C-307","SDA (CS-F)","","","","","OS (CY-A)","","","","","","","","","","OS (DS-A, 24)","","","","","OOP (CS-C, 25)","","","","","OOP (CS-D, 25)","","","","D-306","NLP (DS)","","","","","Stat & Math (DS)",""
'''


def _matched_from_real_tuesday():
    grid = list(csv.reader(io.StringIO(REAL_TUESDAY_CSV)))
    extracted = extract_classes_from_grid(grid, day_of_week=1)

    matched = []
    for cell in extracted:
        parsed = ParsedCell(
            course_text=cell.course_text,
            section=cell.section,
            year_suffix=cell.year_suffix,
            time_override=None,
            cancelled=cell.cancelled,
        )
        result = match_cell(
            parsed,
            MY_ENROLLMENT,
            day_of_week=cell.day_of_week,
            start_time=cell.start_time,
            end_time=cell.end_time,
            room=cell.room,
        )
        if result is not None:
            matched.append(result)
    return matched


def test_real_tuesday_data_matches_all_three_expected_enrolled_classes():
    matched = _matched_from_real_tuesday()
    actual = {(m.enrolled.course_name, m.start_time, m.end_time, m.room) for m in matched}
    assert actual == {
        ("DLD", "08:30", "09:50", "C-308"),
        ("OOP Theory", "14:30", "15:50", "C-307"),
        ("Discrete Structures", "15:55", "17:15", "C-308"),
    }


def test_real_tuesday_data_correctly_ignores_non_enrolled_sections():
    matched = _matched_from_real_tuesday()
    # The real row also contains "Data St (CS-G)", "SDA (CS-G)", "OS (CY-A)",
    # "OS (DS-A, 24)", "OOP (CS-D, 25)", etc. - none are enrolled courses and
    # must never appear in the matched result.
    assert len(matched) == 3


def test_real_tuesday_repeat_courses_use_the_primary_blocks_room_column():
    matched = _matched_from_real_tuesday()
    by_name = {m.enrolled.course_name: m for m in matched}
    # Confirms an earlier structural finding: on the real sheet, these two
    # repeat-course cells physically sit within the primary (C-building)
    # block's own columns - NOT under the "BS Repeat Courses" column-group
    # further right (that group holds unrelated courses in this row, e.g.
    # "DB & OS (CI)"/"NLP (DS)"). Room resolution correctly follows physical
    # column position, matching the row's own C-308/C-307, not an assumed
    # "repeat courses live in the Repeat block" heuristic - which the real
    # data disproves.
    assert by_name["Discrete Structures"].room == "C-308"
    assert by_name["OOP Theory"].room == "C-307"
