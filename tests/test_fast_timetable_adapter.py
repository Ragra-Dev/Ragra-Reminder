import pytest

from ragra.adapters import fast_timetable
from ragra.adapters.fast_timetable import (
    AmbiguousTimetableStructureError,
    FastTimetableClient,
    SheetInfo,
    discover_weekday_tabs,
    discover_weekday_tabs_via_public_names,
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


# --- Zero-credential fallback discovery (no API key configured) ---


def test_public_name_discovery_finds_tabs_whose_own_content_confirms_the_weekday(monkeypatch):
    fake_grids = {
        "Monday": [["Monday", "stuff"]],
        "Tuesday": [["Tuesday", "stuff"]],
    }

    def fake_fetch(spreadsheet_id, *, sheet_name, timeout=15):
        return fake_grids.get(sheet_name)

    monkeypatch.setattr(fast_timetable, "_fetch_public_gviz_csv", fake_fetch)

    tabs = discover_weekday_tabs_via_public_names("fake-id")
    assert tabs[0].title == "Monday"
    assert tabs[1].title == "Tuesday"
    assert tabs[0].sheet_id is None  # gid genuinely unknown in this mode
    assert 2 not in tabs  # Wednesday not present - must not be invented


def test_public_name_discovery_rejects_a_name_hit_whose_content_disagrees(monkeypatch):
    # A tab happens to be titled "Monday" but its own first cell doesn't
    # actually say "Monday" - must not be trusted on title alone.
    def fake_fetch(spreadsheet_id, *, sheet_name, timeout=15):
        if sheet_name == "Monday":
            return [["Some Unrelated Sheet", "stuff"]]
        return None

    monkeypatch.setattr(fast_timetable, "_fetch_public_gviz_csv", fake_fetch)

    tabs = discover_weekday_tabs_via_public_names("fake-id")
    assert 0 not in tabs


def test_client_falls_back_to_public_discovery_without_an_api_key(monkeypatch):
    def fake_fetch(spreadsheet_id, *, sheet_name, timeout=15):
        if sheet_name == "Tuesday":
            return [["Tuesday", "stuff"]]
        return None

    monkeypatch.setattr(fast_timetable, "_fetch_public_gviz_csv", fake_fetch)

    client = FastTimetableClient("fake-id")
    assert not client.has_metadata_access
    tabs = client.discover_tabs()
    assert tabs[1].title == "Tuesday"
