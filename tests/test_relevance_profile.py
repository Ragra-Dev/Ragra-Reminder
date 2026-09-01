from datetime import date

from ragra.relevance.profile import UserAcademicProfile, _expected_semester, load_profile
from ragra.timetable.enrollment import MY_ENROLLMENT, TARGET_PROGRAM


def test_load_profile_wraps_real_enrollment_data():
    profile = load_profile(today=date(2026, 9, 1))

    assert isinstance(profile, UserAcademicProfile)
    assert profile.program == TARGET_PROGRAM
    assert profile.enrolled_courses == [course.course_name for course in MY_ENROLLMENT]
    assert profile.section_labels["OOP Theory"] == "CS-C"
    assert profile.enrollment_config["enrollment"] == MY_ENROLLMENT


def test_load_profile_ignores_user_id_in_phase_0_1():
    default_profile = load_profile(today=date(2026, 9, 1))
    named_profile = load_profile(user_id="test-user", today=date(2026, 9, 1))

    assert default_profile == named_profile


def test_expected_semester_counts_terms_since_enrollment_start():
    # Fall 2025 start -> Fall 2025 is semester 1, Spring 2026 is semester 2,
    # Fall 2026 is semester 3 - the example from the design discussion.
    assert _expected_semester(2025, "FALL", today=date(2025, 9, 1)) == 1
    assert _expected_semester(2025, "FALL", today=date(2026, 2, 1)) == 2
    assert _expected_semester(2025, "FALL", today=date(2026, 9, 1)) == 3


def test_expected_semester_is_never_negative_or_zero_at_start():
    assert _expected_semester(2025, "FALL", today=date(2025, 8, 1)) >= 1


def test_expected_semester_works_for_a_spring_enrollment_start():
    # A Spring-start cohort must derive correctly too, not just Fall.
    assert _expected_semester(2025, "SPRING", today=date(2025, 3, 1)) == 1
    assert _expected_semester(2025, "SPRING", today=date(2025, 9, 1)) == 2
    assert _expected_semester(2025, "SPRING", today=date(2026, 3, 1)) == 3
