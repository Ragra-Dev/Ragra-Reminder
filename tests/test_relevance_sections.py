from ragra.relevance.sections import extract_sections


def test_extracts_plain_section_mention():
    assert extract_sections("Lab 02 Section C").sections == ("C",)


def test_extracts_abbreviated_section_mention():
    assert extract_sections("Sec G").sections == ("G",)


def test_extracts_parenthesized_section_mention():
    assert extract_sections("Assignment 3 (Sec G)").sections == ("G",)


def test_extracts_multiple_explicitly_named_sections():
    assert extract_sections("Section C & D").sections == ("C", "D")
    assert extract_sections("Sections C, D and E").sections == ("C", "D", "E")


def test_chapter_reference_yields_no_token():
    # A digit, not a section letter - "Section 3 of the textbook".
    extraction = extract_sections("Read Section 3 of the textbook")
    assert extraction.sections == ()
    assert not extraction.is_range


def test_course_code_yields_no_token():
    # No "section" keyword present at all.
    extraction = extract_sections("CS-101 Assignment 1")
    assert extraction.sections == ()
    assert not extraction.is_range


def test_range_is_flagged_and_not_expanded():
    extraction = extract_sections("Sections A-D")
    assert extraction.sections == ()
    assert extraction.is_range

    extraction2 = extract_sections("Section A to D")
    assert extraction2.is_range


def test_no_section_token_at_all():
    extraction = extract_sections("Assignment 1 is due Monday")
    assert extraction.sections == ()
    assert not extraction.is_range


def test_case_and_whitespace_insensitive():
    assert extract_sections("section c").sections == ("C",)
    assert extract_sections("SEC  G").sections == ("G",)


def test_bare_plural_word_is_not_misread_as_a_section_letter():
    # Regression: "sections" with nothing following it must not be
    # backtracked into "section" + a bare letter "s".
    for text in ("Announcement for all sections", "See the attached sections.", "New sections added"):
        extraction = extract_sections(text)
        assert extraction.sections == ()
        assert not extraction.is_range


def test_all_sections_phrase_is_flagged():
    for text in ("Announcement for all sections", "This applies to all batches", "all sections and batches"):
        assert extract_sections(text).applies_to_all is True


def test_specific_section_mention_does_not_set_applies_to_all():
    assert extract_sections("Section C").applies_to_all is False
