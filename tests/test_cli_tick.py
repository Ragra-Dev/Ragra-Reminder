"""Tests for FAST timetable sync's integration into the tick pipeline:
isolation from the other three steps, idempotency across repeated runs,
graceful behavior when unconfigured, and that the Sheets API key can never
leak into a log line or error message.
"""

from pathlib import Path

import pytest

from ragra import cli
from ragra.adapters.calendar import CalendarTokenPaths
from ragra.adapters.classroom import ClassroomTokenPaths
from ragra.adapters.fast_timetable import FastTimetableAdapterError, SheetInfo
from ragra.config import Config
from ragra.sync.timetable_sync import TimetableSyncError


def _make_config(tmp_path: Path, *, spreadsheet_id: str | None, sheets_api_key: str | None = None) -> Config:
    return Config(
        ragra_home=tmp_path,
        db_path=tmp_path / "ragra.db",
        hermes_bin=None,
        notify_target=None,
        fast_student_id=None,
        calendar_id="primary",
        calendar_paths=CalendarTokenPaths(tmp_path / "client.json", tmp_path / "token.json"),
        classroom_paths=ClassroomTokenPaths(
            tmp_path / "client.json", tmp_path / "no-such-token.json", tmp_path / "no-such-legacy-token.json"
        ),
        web_host="127.0.0.1",
        web_port=8731,
        sheets_api_key=sheets_api_key,
        fast_timetable_spreadsheet_id=spreadsheet_id,
        smtp_host=None,
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_use_ssl=False,
        email_from=None,
        email_to=None,
        web_base_url=None,
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
    # Must NOT resolve to any real Google credential on this machine - a
    # missing file here forces a clean, fast, deterministic failure instead
    # of this "unit" test making real live Classroom/Calendar API calls.
    monkeypatch.setenv("RAGRA_GOOGLE_CALENDAR_CREDENTIAL_FILE", str(tmp_path / "missing_calendar_token.json"))
    monkeypatch.setenv("RAGRA_GOOGLE_OAUTH_CLIENT_FILE", str(tmp_path / "missing_client.json"))
    monkeypatch.setenv("RAGRA_GOOGLE_CLASSROOM_TOKEN_DIR", str(tmp_path / "no-such-classroom-token-dir"))

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


# --- Structured tick_sessions diagnostics (48-hour retention) ---


def _tick_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGRA_HOME", str(tmp_path))
    monkeypatch.setenv("RAGRA_FAST_TIMETABLE_SPREADSHEET_ID", "fake-id")
    # Must NOT resolve to any real Google credential on this machine - a
    # missing file here forces a clean, fast, deterministic failure instead
    # of these "unit" tests making real live Classroom/Calendar API calls.
    monkeypatch.setenv("RAGRA_GOOGLE_CALENDAR_CREDENTIAL_FILE", str(tmp_path / "missing_calendar_token.json"))
    monkeypatch.setenv("RAGRA_GOOGLE_OAUTH_CLIENT_FILE", str(tmp_path / "missing_client.json"))
    monkeypatch.setenv("RAGRA_GOOGLE_CLASSROOM_TOKEN_DIR", str(tmp_path / "no-such-classroom-token-dir"))


def test_tick_records_a_structured_session_for_a_successful_stage(tmp_path, monkeypatch):
    # Classroom/Calendar are deliberately left unconfigured by _tick_env (no
    # real credential in this sandbox) - this test only needs to prove that
    # a stage which DOES succeed (timetable, via the fake client) is
    # captured correctly in the structured session record, independent of
    # the tick's overall exit code.
    _tick_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeClient(GRID))

    cli.cmd_tick(argparse_namespace())

    from ragra.config import load_config
    from ragra.db import repo
    from ragra.db.connection import connect_closing

    config = load_config()
    with connect_closing(config.db_path) as conn:
        sessions = repo.list_recent_tick_sessions(conn)

    assert len(sessions) == 1
    session = sessions[0]
    assert session["started_at"] is not None
    assert session["finished_at"] is not None
    assert session["duration_seconds"] >= 0
    assert session["timetable_result"] is not None and "Timetable sync:" in session["timetable_result"]
    assert session["classroom_result"] is not None  # captured even though it failed in this sandbox
    # classroom/calendar are expected to fail in this deliberately-unconfigured
    # sandbox; the structured error summary should reflect that, not omit it.
    assert session["error"] is not None
    assert "classroom" in session["error"]


def test_tick_records_a_structured_session_on_failure(tmp_path, monkeypatch):
    _tick_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "ragra.adapters.fast_timetable.FastTimetableClient",
        lambda *a, **k: FakeClient(GRID, fail_with=FastTimetableAdapterError("simulated failure")),
    )

    exit_code = cli.cmd_tick(argparse_namespace())

    from ragra.config import load_config
    from ragra.db import repo
    from ragra.db.connection import connect_closing

    config = load_config()
    with connect_closing(config.db_path) as conn:
        sessions = repo.list_recent_tick_sessions(conn)

    assert exit_code == 1
    assert len(sessions) == 1
    session = sessions[0]
    assert session["exit_code"] == 1
    assert session["error"] is not None
    assert "timetable" in session["error"]


def test_purge_old_tick_sessions_removes_only_rows_past_the_cutoff(conn):
    from datetime import datetime, timedelta, timezone

    from ragra.db import repo

    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=72)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()

    for started_at in (old, recent):
        repo.record_tick_session(
            conn,
            started_at=started_at,
            finished_at=started_at,
            duration_seconds=1.0,
            exit_code=0,
            classroom_result=None,
            calendar_result=None,
            reminders_result=None,
            timetable_result=None,
            error=None,
        )

    cutoff = (now - timedelta(hours=48)).isoformat()
    removed = repo.purge_old_tick_sessions(conn, older_than_iso=cutoff)

    remaining = repo.list_recent_tick_sessions(conn)
    assert removed == 1
    assert len(remaining) == 1
    assert remaining[0]["started_at"] == recent


def test_tick_purges_old_sessions_automatically(tmp_path, monkeypatch):
    _tick_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeClient(GRID))

    from datetime import datetime, timedelta, timezone

    from ragra.config import load_config
    from ragra.db import repo
    from ragra.db.connection import connect_closing

    config = load_config()
    old_started_at = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with connect_closing(config.db_path) as conn:
        repo.record_tick_session(
            conn,
            started_at=old_started_at,
            finished_at=old_started_at,
            duration_seconds=1.0,
            exit_code=0,
            classroom_result=None,
            calendar_result=None,
            reminders_result=None,
            timetable_result=None,
            error=None,
        )

    cli.cmd_tick(argparse_namespace())

    with connect_closing(config.db_path) as conn:
        sessions = repo.list_recent_tick_sessions(conn)

    assert all(s["started_at"] != old_started_at for s in sessions)  # the 72h-old row was purged
    assert len(sessions) == 1  # only this tick's own fresh session remains


def test_tick_sessions_never_touch_application_data(tmp_path, monkeypatch):
    _tick_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeClient(GRID))

    cli.cmd_tick(argparse_namespace())

    from ragra.config import load_config
    from ragra.db.connection import connect_closing

    config = load_config()
    with connect_closing(config.db_path) as conn:
        timetable_count = conn.execute("SELECT COUNT(*) AS c FROM timetable_events").fetchone()["c"]

    assert timetable_count == 1  # only the legitimate synced class - purge never touches app data
