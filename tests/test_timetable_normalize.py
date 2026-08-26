from ragra.timetable.normalize import (
    normalize_course_text,
    normalize_section,
    normalize_weekday_name,
    parse_course_cell,
    resolve_24h_time_sequence,
)


def test_weekday_name_matches_exact_and_renamed_tabs():
    assert normalize_weekday_name("Monday") == 0
    assert normalize_weekday_name("MONDAY") == 0
    assert normalize_weekday_name("Monday Fall 2026") == 0
    assert normalize_weekday_name("Mon") == 0
    assert normalize_weekday_name("Tuesday") == 1
    assert normalize_weekday_name("Wednesday") == 2
    assert normalize_weekday_name("Thursday") == 3
    assert normalize_weekday_name("Friday") == 4
    assert normalize_weekday_name("Saturday") == 5
    assert normalize_weekday_name("Sunday") == 6


def test_weekday_name_returns_none_for_unrecognizable_tab():
    assert normalize_weekday_name("WELCOME") is None
    assert normalize_weekday_name("Legend") is None
    assert normalize_weekday_name("") is None


def test_normalize_section_is_case_and_whitespace_insensitive():
    assert normalize_section("CS-G") == "CS-G"
    assert normalize_section("cs g") == "CS-G"
    assert normalize_section("CS_G") == "CS-G"
    assert normalize_section("  CS   G  ") == "CS-G"


def test_normalize_course_text_collapses_whitespace_and_case():
    assert normalize_course_text("  OOP   Theory ") == "oop theory"
    assert normalize_course_text("OOP") == "oop"


# --- Real cell text captured directly from the live FAST spreadsheet ---


def test_parse_plain_native_course_cell():
    parsed = parse_course_cell("DLD (CS-G)")
    assert parsed is not None
    assert parsed.course_text == "DLD"
    assert parsed.section == "CS-G"
    assert parsed.year_suffix is None
    assert parsed.time_override is None
    assert not parsed.cancelled


def test_parse_repeat_course_cell_with_year_suffix():
    parsed = parse_course_cell("OOP (CS-C, 25)")
    assert parsed is not None
    assert parsed.course_text == "OOP"
    assert parsed.section == "CS-C"
    assert parsed.year_suffix == "25"


def test_parse_repeat_course_cell_without_year_suffix():
    # Real example: OOP Lab (CS-A) - the user's own repeat lab section,
    # proving a repeat entry does not always carry a year suffix.
    parsed = parse_course_cell("OOP Lab (CS-A)")
    assert parsed is not None
    assert parsed.course_text == "OOP Lab"
    assert parsed.section == "CS-A"
    assert parsed.year_suffix is None


def test_parse_cell_with_embedded_time_override():
    parsed = parse_course_cell("UHQ-I&II (CS-G) 02:30-04:20")
    assert parsed is not None
    assert parsed.course_text == "UHQ-I&II"
    assert parsed.section == "CS-G"
    assert parsed.time_override == ("02:30", "04:20")


def test_parse_cell_marks_cancelled():
    parsed = parse_course_cell("PF (CS-G) Cancelled")
    assert parsed is not None
    assert parsed.cancelled is True


def test_parse_cell_returns_none_for_non_course_text():
    assert parse_course_cell("") is None
    assert parse_course_cell("Room/ Time") is None
    assert parse_course_cell("C-308") is None


# --- Real header time-slot sequence captured from the live spreadsheet ---


def test_resolve_24h_time_sequence_matches_real_fast_header():
    raw = [
        "08:30-09:50",
        "10:00-11:20",
        "11:30-12:50",
        "01:00-02:20",
        "02:30-03:50",
        "03:55-05:15",
        "05:20-06:40",
        "06:45-08:05",
    ]
    resolved = resolve_24h_time_sequence(raw)
    assert resolved == [
        ("08:30", "09:50"),
        ("10:00", "11:20"),
        ("11:30", "12:50"),
        ("13:00", "14:20"),
        ("14:30", "15:50"),
        ("15:55", "17:15"),
        ("17:20", "18:40"),
        ("18:45", "20:05"),
    ]


def test_resolve_24h_time_sequence_handles_all_morning_slots():
    raw = ["08:30-09:50", "10:00-11:20"]
    assert resolve_24h_time_sequence(raw) == [("08:30", "09:50"), ("10:00", "11:20")]


def test_resolve_24h_time_sequence_handles_a_slot_that_itself_spans_noon():
    # Real lab-block header: a single long slot ("11:30-02:15") crosses from
    # late morning into early afternoon within itself, not just between slots.
    raw = ["08:30-11:15", "11:30-02:15", "02:30-05:15", "05:20-08:05"]
    assert resolve_24h_time_sequence(raw) == [
        ("08:30", "11:15"),
        ("11:30", "14:15"),
        ("14:30", "17:15"),
        ("17:20", "20:05"),
    ]
