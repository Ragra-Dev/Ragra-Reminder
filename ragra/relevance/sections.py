"""Extract section-letter tokens from free Classroom text (titles,
descriptions), using the same canonical section form as
ragra/timetable/normalize.py's normalize_section - see docs/INTERFACES.md
contract #3 for the ambiguous-case decisions this module implements.

Deliberately keyword-anchored (requires the literal word "sec"/"section"):
this is what makes chapter references ("Section 3 of the textbook", a
digit, not a letter) and course codes ("CS-101", no "section" keyword at
all) fall out as "no token found" for free, rather than needing dedicated
false-positive filters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ragra.timetable.normalize import normalize_section

# "For all sections" / "all batches" - an explicit, unambiguous positive
# signal (not merely "no evidence"): this content is confirmed to apply
# regardless of section. Checked before anything else.
_ALL_SECTIONS = re.compile(r"\ball\s+(?:sections?|batches)\b", re.IGNORECASE)

# "Section C", "Sec G", "(Sec G)", "Sections C & D", "Section C, D and E" -
# one or more letters following the "section" keyword, joined by &/and/,.
# The separator between the keyword and the letter(s) is deliberately
# mandatory (not `\s*`, zero-width): with a zero-width separator, the
# plural word "sections" on its own (nothing following) can be misread by
# regex backtracking as "section" + a bare letter "s" - i.e. the plural
# marker itself gets captured as if it were a section label. Requiring a
# real separator character closes that off: "sections" with nothing after
# it correctly yields no match at all.
_SECTION_MENTION = re.compile(
    r"\bsec(?:tion)?s?\.?[\s:\-]+"
    r"(?P<letters>[A-Za-z](?:\s*(?:,|&|and)\s*[A-Za-z])*)\b",
    re.IGNORECASE,
)

# "Sections A-D" / "Section A to D" - a range. Decided: never expanded into
# individual letters, always surfaced as ambiguous.
_SECTION_RANGE = re.compile(
    r"\bsec(?:tion)?s?\.?\s*[:\-]?\s*[A-Za-z]\s*(?:-|to)\s*[A-Za-z]\b",
    re.IGNORECASE,
)

_LETTER_SPLIT = re.compile(r"\s*(?:,|&|and)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class SectionExtraction:
    sections: tuple[str, ...]  # normalized section letters found, e.g. ("C",) or ("C", "D")
    is_range: bool  # True if a "Sections A-D" style range was seen
    applies_to_all: bool = False  # True if an "all sections"/"all batches" phrase was seen


def extract_sections(text: str) -> SectionExtraction:
    """Extract section letters mentioned in `text`. Returns an empty
    `sections` tuple for chapter references, course codes, and text with no
    section token at all - callers must treat that as "no evidence", never
    guess. Returns `is_range=True` (with no sections) for a range mention -
    callers must treat that as ambiguous, never expand it. Returns
    `applies_to_all=True` for an explicit "all sections"/"all batches"
    phrase - callers must treat that as confirmed-relevant, not merely
    unambiguous-but-unproven."""
    if _ALL_SECTIONS.search(text):
        return SectionExtraction(sections=(), is_range=False, applies_to_all=True)

    if _SECTION_RANGE.search(text):
        return SectionExtraction(sections=(), is_range=True)

    match = _SECTION_MENTION.search(text)
    if not match:
        return SectionExtraction(sections=(), is_range=False)

    letters = [letter for letter in _LETTER_SPLIT.split(match.group("letters")) if letter]
    normalized = tuple(dict.fromkeys(normalize_section(letter) for letter in letters))
    return SectionExtraction(sections=normalized, is_range=False)
