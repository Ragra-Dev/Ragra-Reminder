"""Per-user academic profiles.

The property that matters: a user's enrollment is theirs. Before P3 it was
a module constant, and the failure a permissive default produces here is
not a crash - it is a new account being quietly told that somebody else's
courses and sections are theirs, and then being notified about somebody
else's classes.
"""

from __future__ import annotations

from datetime import date

import pytest

from ragra.relevance import profile as profile_module
from ragra.relevance.profile import (
    adopt_legacy_profile,
    load_profile,
    save_profile,
)
from ragra.timetable.enrollment import (
    REGULAR,
    REPEAT,
    EnrolledCourse,
)
from tests.support import make_user, owner_id

TODAY = date(2026, 9, 1)

BEA_ENROLLMENT = (
    EnrolledCourse("Signals and Systems", "EE-A", REGULAR, batch_year="2024", aliases=("S&S",)),
    EnrolledCourse("Circuit Analysis", "EE-B", REPEAT),
)


@pytest.fixture
def alice(conn) -> int:
    return owner_id(conn)


@pytest.fixture
def bea(conn) -> int:
    return make_user(conn, google_sub="profile-second", display_name="Bea")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_a_new_user_does_not_inherit_the_previous_owners_enrollment(conn, alice, bea):
    """The whole point. A permissive default here would tell a new account
    that another person's courses are theirs."""
    inherited = load_profile(conn, user_id=bea, today=TODAY)

    assert inherited.enrolled_courses == []
    assert inherited.section_labels == {}
    assert inherited.enrollment_config["enrollment"] == ()


def test_an_empty_profile_suppresses_nothing(conn, bea):
    """Degrading safely means falling open: with no section labels the
    relevance engine has nothing to match against, so it must not decide
    that everything belongs to another section."""
    from ragra.relevance.engine import RelevanceDecision, is_relevant

    empty = load_profile(conn, user_id=bea, today=TODAY)
    decision = is_relevant(
        "Quiz for section CS-G tomorrow", "Bring your notes.", "Digital Logic Design", empty
    )

    assert decision is not RelevanceDecision.OTHER_SECTION


def test_two_users_profiles_do_not_overwrite_each_other(conn, alice, bea):
    save_profile(
        conn, user_id=bea, program="EE", batch_year="2024",
        enrollment_start_year=2024, enrollment_start_term="FALL",
        enrollment=BEA_ENROLLMENT,
    )

    assert load_profile(conn, user_id=bea, today=TODAY).program == "EE"
    # Alice still has hers, unaffected.
    assert load_profile(conn, user_id=alice, today=TODAY).program == profile_module.TARGET_PROGRAM


def test_deleting_a_user_removes_their_profile(conn, alice, bea):
    save_profile(
        conn, user_id=bea, program="EE", batch_year="2024",
        enrollment_start_year=2024, enrollment_start_term="FALL",
        enrollment=BEA_ENROLLMENT,
    )

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users WHERE id = ?", (bea,))
    conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM user_profiles WHERE user_id = ?", (bea,)
    ).fetchone()["c"]
    assert remaining == 0


# ---------------------------------------------------------------------------
# The legacy owner
# ---------------------------------------------------------------------------


def test_the_pre_identity_owner_keeps_the_configuration_they_already_had(conn, alice):
    """Nothing about the existing single user's behaviour may change just
    because storage moved."""
    loaded = load_profile(conn, user_id=alice, today=TODAY)

    assert loaded.program == profile_module.TARGET_PROGRAM
    assert loaded.enrolled_courses == [c.course_name for c in profile_module.MY_ENROLLMENT]


def test_signing_in_for_the_first_time_does_not_empty_the_owners_enrollment(conn, alice):
    """The ordering bug this guards: linking the account is exactly what
    stops it matching the "unlinked owner" fallback, so the profile has to
    become real data *before* the link, not after. Getting this wrong would
    silently wipe the owner's enrollment on their very first sign-in."""
    adopt_legacy_profile(conn, user_id=alice)
    conn.execute(
        "UPDATE users SET google_sub = 'now-linked' WHERE id = ?", (alice,)
    )
    conn.commit()

    after = load_profile(conn, user_id=alice, today=TODAY)
    assert after.enrolled_courses == [c.course_name for c in profile_module.MY_ENROLLMENT]


def test_adopting_is_idempotent(conn, alice):
    adopt_legacy_profile(conn, user_id=alice)
    adopt_legacy_profile(conn, user_id=alice)

    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM user_profiles WHERE user_id = ?", (alice,)
    ).fetchone()["c"]
    assert rows == 1


def test_calling_without_a_connection_still_returns_the_legacy_profile():
    """Kept so callers that genuinely have no database - the relevance
    engine's own tests among them - are unaffected."""
    assert load_profile(today=TODAY).program == profile_module.TARGET_PROGRAM


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_a_saved_profile_round_trips_exactly(conn, bea):
    save_profile(
        conn, user_id=bea, program="EE", batch_year="2024",
        enrollment_start_year=2024, enrollment_start_term="SPRING",
        enrollment=BEA_ENROLLMENT,
    )

    loaded = load_profile(conn, user_id=bea, today=TODAY)

    assert loaded.program == "EE"
    assert loaded.enrolled_courses == ["Signals and Systems", "Circuit Analysis"]
    assert loaded.section_labels == {
        "Signals and Systems": "EE-A",
        "Circuit Analysis": "EE-B",
    }
    restored = loaded.enrollment_config["enrollment"]
    assert restored == BEA_ENROLLMENT
    assert restored[0].aliases == ("S&S",)
    assert restored[1].enrollment_type == REPEAT


def test_saving_again_replaces_rather_than_merges(conn, bea):
    """A profile is edited as a whole. A merge would leave an enrollment
    half from one semester and half from the next."""
    save_profile(
        conn, user_id=bea, program="EE", batch_year="2024",
        enrollment_start_year=2024, enrollment_start_term="FALL",
        enrollment=BEA_ENROLLMENT,
    )
    save_profile(
        conn, user_id=bea, program="EE", batch_year="2024",
        enrollment_start_year=2024, enrollment_start_term="FALL",
        enrollment=(EnrolledCourse("Thermodynamics", "ME-A", REGULAR),),
    )

    loaded = load_profile(conn, user_id=bea, today=TODAY)
    assert loaded.enrolled_courses == ["Thermodynamics"]


def test_expected_semester_counts_from_the_stored_start_term(conn, bea):
    save_profile(
        conn, user_id=bea, program="EE", batch_year="2024",
        enrollment_start_year=2025, enrollment_start_term="FALL",
        enrollment=(),
    )

    # Fall 2025 is semester 1; Fall 2026 is semester 3.
    assert load_profile(conn, user_id=bea, today=date(2025, 9, 1)).expected_semester == 1
    assert load_profile(conn, user_id=bea, today=date(2026, 3, 1)).expected_semester == 2
    assert load_profile(conn, user_id=bea, today=date(2026, 9, 1)).expected_semester == 3


def test_an_invalid_start_term_is_rejected(conn, bea):
    with pytest.raises(ValueError):
        save_profile(
            conn, user_id=bea, program="EE", batch_year=None,
            enrollment_start_year=2024, enrollment_start_term="SUMMER",
            enrollment=(),
        )


def test_an_invalid_enrollment_type_is_still_rejected_on_read(conn, bea):
    """Stored JSON goes back through EnrolledCourse's own validation, so a
    hand-edited row cannot introduce a value the matcher does not
    understand."""
    conn.execute(
        """INSERT INTO user_profiles
             (user_id, program, batch_year, enrollment_start_year, enrollment_start_term,
              enrollment, created_at, updated_at)
           VALUES (?, 'EE', NULL, 2024, 'FALL', ?, '2026-01-01T00:00:00+00:00',
                   '2026-01-01T00:00:00+00:00')""",
        (bea, '[{"course_name": "X", "section": "A", "enrollment_type": "SOMETIMES"}]'),
    )
    conn.commit()

    with pytest.raises(ValueError):
        load_profile(conn, user_id=bea, today=TODAY)


def test_unknown_stored_fields_are_ignored_rather_than_fatal(conn, bea):
    """A profile written by a later version must not make an older one
    unable to read the account at all."""
    conn.execute(
        """INSERT INTO user_profiles
             (user_id, program, batch_year, enrollment_start_year, enrollment_start_term,
              enrollment, created_at, updated_at)
           VALUES (?, 'EE', NULL, 2024, 'FALL', ?, '2026-01-01T00:00:00+00:00',
                   '2026-01-01T00:00:00+00:00')""",
        (
            bea,
            '[{"course_name": "X", "section": "A", "enrollment_type": "REGULAR",'
            ' "credits_from_a_future_version": 3}]',
        ),
    )
    conn.commit()

    loaded = load_profile(conn, user_id=bea, today=TODAY)
    assert loaded.enrolled_courses == ["X"]
