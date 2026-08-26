"""Tests for FAST timetable sync's integration into the tick pipeline:
isolation from the other three steps, idempotency across repeated runs,
graceful behavior when unconfigured, and that the Sheets API key can never
leak into a log line or error message.
"""

from pathlib import Path

import pytest

from ragra import cli
from ragra.adapters.calendar import CalendarTokenPaths
from ragra.adapters.fast_timetable import FastTimetableAdapterError, SheetInfo
from ragra.config import Config
from ragra.sync.timetable_sync import TimetableSyncError


def _make_config(tmp_path: Path, *, spreadsheet_id: str | None, sheets_api_key: str | None = None) -> Config:
    return Config(
        ragra_home=tmp_path,
        db_path=tmp_path / "ragra.db",
        hermes_repo_path=None,
        hermes_bin=None,
        notify_target=None,
        fast_student_id=None,
        calendar_id="primary",
        calendar_paths=CalendarTokenPaths(tmp_path / "client.json", tmp_path / "token.json"),
        web_host="127.0.0.1",
        web_port=8731,
        sheets_api_key=sheets_api_key,
        fast_timetable_spreadsheet_id=spreadsheet_id,
    )


class FakeClient:
    """Duck-types FastTimetableClient with a single controllable weekday tab."""

    def __init__(self, grid, *, fail_with: Exception | None = None):
        self._grid = grid
        self._fail_with = fail_with

    def discover_tabs(self):
        if self._fail_with:
            raise self._fail_with
        return {0: SheetInfo(title="Monday", sheet_id=1)}

    def get_values(self, sheet_title):
        return self._grid


GRID = [
    ["Monday", ""],
    ["Room/ Time", "08:30-09:50"],
    ["C-311", "DLD (CS-G)"],
]

ENROLLMENT = None  # use the real default enrollment via sync_timetable's default


def _log_capture():
    lines: list[str] = []
    return lines, lines.append


# --- _run_timetable_sync: unit level ---


def test_skips_gracefully_when_spreadsheet_id_not_configured(conn, tmp_path):
    config = _make_config(tmp_path, spreadsheet_id=None)
    lines, log = _log_capture()

    rc, error = cli._run_timetable_sync(conn, config, log)

    assert rc == 0
    assert error is None
    assert any("not set" in line for line in lines)


def test_success_logs_a_single_summary_line(conn, tmp_path, monkeypatch):
    config = _make_config(tmp_path, spreadsheet_id="fake-id")
    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeClient(GRID)
    )
    lines, log = _log_capture()

    rc, error = cli._run_timetable_sync(conn, config, log)

    assert rc == 0
    assert error is None
    assert any(line.startswith("Timetable sync:") for line in lines)


def test_sync_failure_returns_error_without_raising(conn, tmp_path, monkeypatch):
    config = _make_config(tmp_path, spreadsheet_id="fake-id")
    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient",
        lambda *a, **k: FakeClient(GRID, fail_with=FastTimetableAdapterError("network down")),
    )
    lines, log = _log_capture()

    rc, error = cli._run_timetable_sync(conn, config, log)

    assert rc == 1
    assert error is not None
    assert any("Timetable sync failed" in line for line in lines)


def test_unexpected_exception_is_contained_not_propagated(conn, tmp_path, monkeypatch):
    config = _make_config(tmp_path, spreadsheet_id="fake-id")
    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient",
        lambda *a, **k: FakeClient(GRID, fail_with=RuntimeError("boom")),
    )
    lines, log = _log_capture()

    rc, error = cli._run_timetable_sync(conn, config, log)

    assert rc == 1
    assert error is not None


def test_repeated_calls_are_idempotent(conn, tmp_path, monkeypatch):
    config = _make_config(tmp_path, spreadsheet_id="fake-id")
    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeClient(GRID)
    )

    lines1, log1 = _log_capture()
    rc1, _ = cli._run_timetable_sync(conn, config, log1)
    lines2, log2 = _log_capture()
    rc2, _ = cli._run_timetable_sync(conn, config, log2)

    assert rc1 == 0 and rc2 == 0
    first_summary = next(line for line in lines1 if line.startswith("Timetable sync:"))
    assert "1 new" in first_summary  # first run created the one class

    second_summary = next(line for line in lines2 if line.startswith("Timetable sync:"))
    assert "0 new" in second_summary
    assert "0 updated" in second_summary

    total = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert total == 1  # no duplicates across the two calls


def test_ambiguous_structure_does_not_wipe_existing_data(conn, tmp_path, monkeypatch):
    config = _make_config(tmp_path, spreadsheet_id="fake-id")
    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeClient(GRID)
    )
    _, log = _log_capture()
    cli._run_timetable_sync(conn, config, log)
    before = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert before == 1

    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient",
        lambda *a, **k: FakeClient(GRID, fail_with=TimetableSyncError("no weekday tabs found")),
    )
    _, log2 = _log_capture()
    rc, error = cli._run_timetable_sync(conn, config, log2)

    assert rc == 1
    after = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]
    assert after == before  # untouched, not wiped


def test_api_key_never_appears_in_a_failure_log_line(conn, tmp_path, monkeypatch):
    config = _make_config(tmp_path, spreadsheet_id="fake-id", sheets_api_key="super-secret-key-value")
    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient",
        lambda *a, **k: FakeClient(
            GRID, fail_with=FastTimetableAdapterError("request to ...?key=super-secret-key-value failed")
        ),
    )
    lines, log = _log_capture()

    rc, error = cli._run_timetable_sync(conn, config, log)

    assert rc == 1
    assert not any("super-secret-key-value" in line for line in lines)
    assert error is None or "super-secret-key-value" not in error


# --- Full tick integration ---


def test_tick_includes_timetable_step_and_isolates_its_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGRA_HOME", str(tmp_path))
    monkeypatch.setenv("RAGRA_FAST_TIMETABLE_SPREADSHEET_ID", "fake-id")
    monkeypatch.delenv("HERMES_REPO_PATH", raising=False)
    monkeypatch.setenv("RAGRA_GOOGLE_CALENDAR_CREDENTIAL_FILE", str(tmp_path / "missing_calendar_token.json"))
    monkeypatch.setenv("RAGRA_GOOGLE_OAUTH_CLIENT_FILE", str(tmp_path / "missing_client.json"))

    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient",
        lambda *a, **k: FakeClient(GRID, fail_with=FastTimetableAdapterError("simulated failure")),
    )

    exit_code = cli.cmd_tick(argparse_namespace())

    from ragra.config import load_config
    from ragra.db.connection import connect_closing

    config = load_config()
    with connect_closing(config.db_path) as conn:
        health_rows = {
            row["component"]: row["consecutive_failures"]
            for row in conn.execute("SELECT component, consecutive_failures FROM pipeline_health")
        }
    assert "timetable" in health_rows
    assert health_rows["timetable"] >= 1  # the simulated failure was tracked

    log_path = tmp_path / "logs" / "ragra.log"
    assert log_path.exists()
    log_text = log_path.read_text(encoding="utf-8")
    assert "tick end" in log_text  # tick completed rather than crashing
    assert exit_code == 1  # a real failure is reported


def argparse_namespace():
    import argparse

    return argparse.Namespace()
