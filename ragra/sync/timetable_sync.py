"""FAST timetable sync: RECONCILE + STORE stage.

Orchestrates the adapter (SCRAPE, ragra/adapters/fast_timetable.py) and
enrollment matching (IDENTIFY, ragra/timetable/match.py) into idempotent
persistence. This module deliberately contains no cell-parsing or
column-position logic of its own - if that ever needs to change, it
changes in the adapter, not here.

Self-healing: nothing is written to timetable_events unless the whole
scrape is structurally sound (weekday tabs discoverable, no ambiguous
legend/tab names, every tab readable). A malformed or partial scrape raises
TimetableSyncError before touching any stored row - the last-known-good
timetable is never silently replaced with an empty or partial result.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ragra.adapters.fast_timetable import (
    AmbiguousTimetableStructureError,
    ExtractedClass,
    FastTimetableAdapterError,
    FastTimetableClient,
    SheetInfo,
    extract_classes_from_grid,
)
from ragra.db import repo
from ragra.relevance.profile import load_profile
from ragra.timetable.enrollment import EnrolledCourse
from ragra.timetable.match import AmbiguousMatchError, match_cell
from ragra.timetable.normalize import ParsedCell, normalize_course_text, normalize_section


class TimetableSyncError(RuntimeError):
    """The scrape was not structurally sound enough to trust. Nothing was
    written; the previously stored timetable (if any) is untouched."""


@dataclass
class TimetableSyncSummary:
    classes_found: int = 0
    classes_created: int = 0
    classes_updated: int = 0
    classes_unchanged: int = 0
    classes_cancelled: int = 0
    unmatched_ambiguous: list[str] = field(default_factory=list)


def _external_id(*, program: str, batch_year: str | None, section: str, course_name: str, occurrence_index: int) -> str:
    batch_component = batch_year if batch_year else "REPEAT"
    return (
        f"{program}:{batch_component}:{normalize_section(section)}:"
        f"{normalize_course_text(course_name)}:{occurrence_index}"
    )


def _extracted_to_parsed_cell(cell: ExtractedClass) -> ParsedCell:
    return ParsedCell(
        course_text=cell.course_text,
        section=cell.section,
        year_suffix=cell.year_suffix,
        time_override=None,
        cancelled=cell.cancelled,
    )


def sync_timetable(
    conn: sqlite3.Connection,
    client: FastTimetableClient,
    *,
    user_id: int,
    spreadsheet_id: str,
    enrollment: tuple[EnrolledCourse, ...] | None = None,
) -> TimetableSyncSummary:
    """Reconcile the shared FAST timetable into ONE user's timetable_events.

    The spreadsheet itself is a public university artifact, but which of its
    classes belong to a person is decided by that person's enrollment, so
    every stored row is owned: two users in different sections legitimately
    derive different (and overlapping) external_ids from the same source
    sheet, which is why migration 0015 made external_id unique per user
    rather than globally."""
    profile = load_profile(conn, user_id=user_id)
    if enrollment is None:
        enrollment = profile.enrollment_config["enrollment"]
    repo.record_sync_start(conn, user_id=user_id, source="timetable")

    try:
        weekday_tabs = client.discover_tabs()
    except (FastTimetableAdapterError, AmbiguousTimetableStructureError) as exc:
        repo.record_sync_error(conn, user_id=user_id, source="timetable", error=str(exc))
        raise TimetableSyncError(str(exc)) from exc

    if not weekday_tabs:
        msg = "No weekday tabs could be identified in the FAST timetable source."
        repo.record_sync_error(conn, user_id=user_id, source="timetable", error=msg)
        raise TimetableSyncError(msg)

    day_grids: list[tuple[int, SheetInfo, list[ExtractedClass]]] = []
    for day, sheet in sorted(weekday_tabs.items()):
        try:
            grid = client.get_values(sheet.title)
        except FastTimetableAdapterError as exc:
            repo.record_sync_error(conn, user_id=user_id, source="timetable", error=str(exc))
            raise TimetableSyncError(str(exc)) from exc
        day_grids.append((day, sheet, extract_classes_from_grid(grid, day_of_week=day)))

    summary = TimetableSyncSummary()

    # Group matches by enrolled-course identity so occurrence_index can be
    # assigned deterministically (sorted by day/time), independent of scrape
    # order - this is what makes a weekly-occurrence identity stable.
    grouped: dict[tuple[str, str, str], list[tuple[SheetInfo, object]]] = {}
    for _day, sheet, extracted in day_grids:
        for cell in extracted:
            try:
                matched = match_cell(
                    _extracted_to_parsed_cell(cell),
                    enrollment,
                    day_of_week=cell.day_of_week,
                    start_time=cell.start_time,
                    end_time=cell.end_time,
                    room=cell.room,
                )
            except AmbiguousMatchError as exc:
                summary.unmatched_ambiguous.append(str(exc))
                continue
            if matched is None:
                continue
            key = (matched.enrolled.course_name, matched.enrolled.section, matched.enrolled.enrollment_type)
            grouped.setdefault(key, []).append((sheet, matched))

    seen_external_ids: set[str] = set()
    for (course_name, section, enrollment_type), entries in grouped.items():
        entries.sort(key=lambda pair: (pair[1].day_of_week, pair[1].start_time))
        enrolled = entries[0][1].enrolled
        for occurrence_index, (sheet, matched) in enumerate(entries):
            summary.classes_found += 1
            external_id = _external_id(
                program=profile.program,
                batch_year=enrolled.batch_year,
                section=section,
                course_name=course_name,
                occurrence_index=occurrence_index,
            )
            seen_external_ids.add(external_id)
            status = "CANCELLED" if matched.cancelled else "SCHEDULED"
            result = repo.upsert_timetable_event(
                conn,
                user_id=user_id,
                external_id=external_id,
                course_name=course_name,
                program=profile.program,
                batch_year=enrolled.batch_year,
                enrollment_type=enrollment_type,
                day_of_week=matched.day_of_week,
                occurrence_index=occurrence_index,
                start_time=matched.start_time,
                end_time=matched.end_time,
                room=matched.room,
                instructor=None,
                section=section,
                status=status,
                source_spreadsheet_id=spreadsheet_id,
                source_sheet_gid=str(sheet.sheet_id) if sheet.sheet_id is not None else None,
                source_sheet_title=sheet.title,
            )
            if result.created:
                summary.classes_created += 1
            elif result.changed_fields:
                summary.classes_updated += 1
            else:
                summary.classes_unchanged += 1

    summary.classes_cancelled = len(
        repo.cancel_timetable_events_missing_from_source(
            conn, user_id=user_id, seen_external_ids=seen_external_ids
        )
    )

    repo.record_sync_success(conn, user_id=user_id, source="timetable")
    return summary
