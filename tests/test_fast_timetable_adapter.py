import pytest

from ragra.adapters.fast_timetable import (
    AmbiguousTimetableStructureError,
    SheetInfo,
    discover_weekday_tabs,
)


def test_discovers_weekday_tabs_by_name_ignoring_non_weekday_tabs():
    sheets = [
        SheetInfo(title="WELCOME", sheet_id=1174567785),
        SheetInfo(title="MONDAY", sheet_id=1882612924),
        SheetInfo(title="TUESDAY", sheet_id=945396749),
        SheetInfo(title="WEDNESDAY", sheet_id=542677125),
        SheetInfo(title="THURSDAY", sheet_id=571927841),
        SheetInfo(title="FRIDAY", sheet_id=1783333514),
        SheetInfo(title="SATURDAY", sheet_id=1949393871),
    ]
    tabs = discover_weekday_tabs(sheets)
    assert tabs[0].title == "MONDAY"
    assert tabs[1].title == "TUESDAY"
    assert tabs[4].title == "FRIDAY"
    assert 6 not in tabs  # no Sunday tab present - must not be invented


def test_discovers_renamed_weekday_tabs():
    sheets = [
        SheetInfo(title="Monday Fall 2026", sheet_id=1),
        SheetInfo(title="Mon", sheet_id=2),
    ]
    # Two DIFFERENT tabs both resolving to Monday is exactly the ambiguity
    # case - it must be reported, not silently resolved.
    with pytest.raises(AmbiguousTimetableStructureError):
        discover_weekday_tabs(sheets)


def test_single_renamed_tab_still_resolves():
    sheets = [SheetInfo(title="Monday Fall 2026", sheet_id=1)]
    tabs = discover_weekday_tabs(sheets)
    assert tabs[0].title == "Monday Fall 2026"


def test_gids_are_never_required_for_discovery():
    # Deliberately use gid values that don't match any "real" ones from any
    # prior semester - discovery must work purely from the title text.
    sheets = [SheetInfo(title="Tuesday", sheet_id=999999)]
    tabs = discover_weekday_tabs(sheets)
    assert tabs[1].sheet_id == 999999
