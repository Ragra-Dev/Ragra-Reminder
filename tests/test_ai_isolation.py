"""Regression tests for the Core/Optional architecture boundary:

    Ragra Core                       Optional Features
    ├── Classroom                    └── AI Advisor
    ├── FAST
    ├── Calendar
    ├── Reminder Engine
    └── Notification Layer

Core sync/reminder/tick modules must never import the AI package, and must
keep working - including a real `tick` - with the entire `ragra.ai` package
unavailable. The AI feature's own entrypoints (`ragra plan`, `ragra brief
--ai`) must degrade to a clear, user-facing message rather than a traceback
when AI is unavailable/unconfigured, and must never touch sync/reminder
state or trigger a browser/auth flow.
"""

from __future__ import annotations

import argparse
import importlib
import io
import logging
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from ragra import cli
from ragra.adapters.calendar import CalendarTokenPaths
from ragra.adapters.classroom import ClassroomTokenPaths
from ragra.adapters.fast_timetable import SheetInfo
from ragra.config import Config

# Core modules that must never reach for the optional AI package, by source
# inspection - catches a reintroduced import even if no test happens to
# exercise the exact code path that would trigger it. ragra.cli is
# deliberately excluded: it legitimately hosts the AI feature's own explicit
# entrypoints (`plan`, `brief --ai`), each importing the AI package locally
# within its own command function - that boundary is instead verified
# behaviorally below (tick/sync/dispatch keep working with AI unavailable).
CORE_MODULES = [
    "ragra.reminders.dispatch",
    "ragra.reminders.engine",
    "ragra.sync.classroom_sync",
    "ragra.sync.calendar_sync",
    "ragra.sync.timetable_sync",
    "ragra.health",
    "ragra.web.app",
]


@pytest.fixture(autouse=True)
def _isolated_ragra_logger():
    """`ragra.logging_setup.configure_logging` attaches handlers to the
    process-global "ragra" logger only once per process. Reset it around
    each test here so a `cmd_tick` call in this file never leaves a stale
    handler (pointed at a since-deleted tmp_path) for other test modules."""
    logger = logging.getLogger("ragra")
    saved_handlers = list(logger.handlers)
    logger.handlers = []
    yield
    for handler in logger.handlers:
        handler.close()
    logger.handlers = saved_handlers


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_core_module_source_never_references_ai_package(module_name):
    module = importlib.import_module(module_name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "ragra.ai" not in source
    assert "ragra_ai" not in source


def _poison_ai_package(monkeypatch):
    """Simulate the entire ragra.ai package being unavailable (uninstalled
    or broken), not just unconfigured - any `import ragra.ai...` raises
    ImportError, matching Python's own behavior for a sys.modules entry of
    None."""
    monkeypatch.setitem(sys.modules, "ragra.ai", None)
    monkeypatch.setitem(sys.modules, "ragra.ai.advisor", None)


def _make_config(tmp_path: Path, *, spreadsheet_id: str | None = None) -> Config:
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
        sheets_api_key=None,
        fast_timetable_spreadsheet_id=spreadsheet_id,
        telegram_bot_token=None,
        telegram_chat_id=None,
    )


class FakeTimetableClient:
    """Duck-types FastTimetableClient with a single controllable weekday tab."""

    def discover_tabs(self):
        return {0: SheetInfo(title="Monday", sheet_id=1)}

    def get_values(self, sheet_title):
        return [
            ["Monday", ""],
            ["Room/ Time", "08:30-09:50"],
            ["C-311", "DLD (CS-G)"],
        ]


def _tick_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGRA_HOME", str(tmp_path))
    monkeypatch.setenv("RAGRA_FAST_TIMETABLE_SPREADSHEET_ID", "fake-id")
    # Force clean, fast, deterministic Classroom/Calendar failures instead of
    # this test making real live API calls.
    monkeypatch.setenv("RAGRA_GOOGLE_CALENDAR_CREDENTIAL_FILE", str(tmp_path / "missing_calendar_token.json"))
    monkeypatch.setenv("RAGRA_GOOGLE_OAUTH_CLIENT_FILE", str(tmp_path / "missing_client.json"))
    monkeypatch.setenv("RAGRA_GOOGLE_CLASSROOM_TOKEN_DIR", str(tmp_path / "no-such-classroom-token-dir"))


def test_core_tick_runs_with_ai_package_unavailable(tmp_path, monkeypatch):
    _tick_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeTimetableClient())
    _poison_ai_package(monkeypatch)

    exit_code = cli.cmd_tick(argparse.Namespace())

    log_path = tmp_path / "logs" / "ragra.log"
    assert log_path.exists()
    log_text = log_path.read_text(encoding="utf-8")
    assert "tick end" in log_text  # tick completed rather than crashing on the poisoned AI import
    # timetable succeeds regardless of AI availability; classroom/calendar
    # fail only because this sandbox has no real credentials, not because of AI.
    assert exit_code == 1
    assert "Timetable sync:" in log_text


def test_reminders_command_works_with_ai_package_unavailable(tmp_path, monkeypatch):
    _poison_ai_package(monkeypatch)
    monkeypatch.setenv("RAGRA_HOME", str(tmp_path))

    with cli.connect_closing(_make_config(tmp_path).db_path) as conn:
        from ragra.reminders.dispatch import dispatch_due_reminders

        # Must not raise ImportError/AttributeError even though ragra.ai is poisoned.
        summary = dispatch_due_reminders(conn, hermes_bin=None, notify_target=None, now="2026-01-01T00:00:00+00:00")

    assert summary.errors == []


def test_sync_stages_work_with_ai_package_unavailable(tmp_path, monkeypatch):
    _tick_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ragra.adapters.fast_timetable.FastTimetableClient", lambda *a, **k: FakeTimetableClient())
    _poison_ai_package(monkeypatch)

    config = _make_config(tmp_path, spreadsheet_id="fake-id")
    with cli.connect_closing(config.db_path) as conn:
        rc, error = cli._run_timetable_sync(conn, config, print)

    assert rc == 0
    assert error is None


def test_plan_command_fails_gracefully_when_ai_package_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGRA_HOME", str(tmp_path))
    _poison_ai_package(monkeypatch)

    out = io.StringIO()
    with redirect_stdout(out):
        exit_code = cli.cmd_plan(argparse.Namespace())

    assert exit_code == 1
    assert "AI advisor is not available" in out.getvalue()
    # No traceback/crash - a plain, user-facing line only.
    assert "Traceback" not in out.getvalue()


def test_plan_command_fails_gracefully_when_ai_unconfigured(tmp_path, monkeypatch):
    # AI package present, but no HERMES_BIN configured - the other half of
    # "unavailable or unconfigured" from the graceful-failure requirement.
    monkeypatch.setenv("RAGRA_HOME", str(tmp_path))

    out = io.StringIO()
    with redirect_stdout(out):
        exit_code = cli.cmd_plan(argparse.Namespace())

    assert exit_code == 1
    assert "AI advisory unavailable" in out.getvalue()
    assert "Traceback" not in out.getvalue()


def test_plan_command_never_opens_a_browser_or_touches_sync_state(tmp_path, monkeypatch):
    # Regression guard: an unavailable/unconfigured AI command must not
    # trigger any interactive auth flow. classroom_adapter.get_classroom_client
    # would be the only thing capable of opening a browser here - it must
    # simply never be called by cmd_plan.
    monkeypatch.setenv("RAGRA_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        "ragra.adapters.classroom.get_classroom_client",
        lambda *a, **k: calls.append(1),
    )

    with redirect_stdout(io.StringIO()):
        cli.cmd_plan(argparse.Namespace())

    assert calls == []
