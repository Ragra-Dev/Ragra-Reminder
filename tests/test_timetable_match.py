import pytest

from ragra.timetable.enrollment import REGULAR, REPEAT, EnrolledCourse
from ragra.timetable.match import AmbiguousMatchError, match_cell
from ragra.timetable.normalize import parse_course_cell

ENROLLMENT = (
    EnrolledCourse("Linear Algebra", "CS-G", REGULAR, batch_year="2025", aliases=("LA",)),
    EnrolledCourse("DLD", "CS-G", REGULAR, batch_year="2025"),
    EnrolledCourse("Discrete Structures", "CS-B", REPEAT, aliases=("Discrete",)),
    EnrolledCourse("OOP Theory", "CS-C", REPEAT, aliases=("OOP",)),
    EnrolledCourse("OOP Lab", "CS-A", REPEAT),
)


def _match(raw_cell: str, **kwargs):
    parsed = parse_course_cell(raw_cell)
    assert parsed is not None
    defaults = dict(day_of_week=0, start_time="08:30", end_time="09:50", room="C-311")
    defaults.update(kwargs)
    return match_cell(parsed, ENROLLMENT, **defaults)


def test_matches_regular_course_by_course_name_and_section():
    result = _match("DLD (CS-G)")
    assert result is not None
    assert result.enrolled.course_name == "DLD"
    assert result.enrolled.enrollment_type == REGULAR
    assert result.enrolled.batch_year == "2025"


def test_matches_regular_course_via_alias():
    result = _match("LA (CS-G)")
    assert result is not None
    assert result.enrolled.course_name == "Linear Algebra"


def test_matches_repeat_theory_course():
    result = _match("OOP (CS-C, 25)")
    assert result is not None
    assert result.enrolled.course_name == "OOP Theory"
    assert result.enrolled.enrollment_type == REPEAT
    assert result.enrolled.batch_year is None


def test_repeat_theory_and_repeat_lab_use_independent_sections():
    theory = _match("OOP (CS-C, 25)")
    lab = _match("OOP Lab (CS-A)")
    assert theory is not None and lab is not None
    assert theory.enrolled.section == "CS-C"
    assert lab.enrolled.section == "CS-A"
    assert theory.enrolled.section != lab.enrolled.section
    assert theory.enrolled.enrollment_type == REPEAT
    assert lab.enrolled.enrollment_type == REPEAT


def test_does_not_match_someone_elses_section():
    # DLD (CS-A) is a real, valid section for OTHER students - not enrolled here.
    assert _match("DLD (CS-A)") is None


def test_does_not_match_unrelated_course():
    assert _match("Calculus (CS-G)") is None


def test_repeat_course_without_year_suffix_still_matches():
    result = _match("OOP Lab (CS-A)")
    assert result is not None
    assert result.enrolled.course_name == "OOP Lab"


def test_ambiguous_match_is_reported_not_guessed():
    ambiguous_enrollment = (
        EnrolledCourse("OOP Theory", "CS-C", REPEAT, aliases=("OOP",)),
        EnrolledCourse("Object Oriented Programming", "CS-C", REPEAT, aliases=("OOP",)),
    )
    parsed = parse_course_cell("OOP (CS-C, 25)")
    assert parsed is not None
    with pytest.raises(AmbiguousMatchError):
        match_cell(
            parsed, ambiguous_enrollment, day_of_week=1, start_time="14:30", end_time="15:50", room="C-307"
        )


def test_matched_class_carries_through_cancelled_flag():
    parsed = parse_course_cell("DLD (CS-G) Cancelled")
    assert parsed is not None
    result = match_cell(
        parsed, ENROLLMENT, day_of_week=0, start_time="08:30", end_time="09:50", room="C-311"
    )
    assert result is not None
    assert result.cancelled is True
