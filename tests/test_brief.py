import sys
from datetime import datetime, timedelta, timezone

from ragra.adapters.ai import AIAdapterError
from ragra.brief import build_deterministic_brief, build_full_brief
from ragra.db import repo


def _make_course(conn):
    return repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE",
    )


def test_brief_is_deterministic_and_reflects_real_state(conn):
    # Fixed midday UTC so `now + 3h` and `now - 1d` can never cross a UTC
    # calendar-day boundary - a real wall-clock `now()` here made this test
    # flaky for ~3 hours a day near UTC midnight (see docs/PROJECT_STATUS.md).
    now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    course_id = _make_course(conn)
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-overdue",
        title="Missed Thing", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now - timedelta(days=1)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(),
    )
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-today",
        title="Due Now", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(hours=3)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(),
    )

    text = build_deterministic_brief(conn, now=now)

    assert "Missed Thing" in text
    assert "Due Now" in text
    assert "OVERDUE (1):" in text
    assert "DUE TODAY (1):" in text


def test_brief_never_invents_data_when_database_is_empty(conn):
    now = datetime.now(timezone.utc)
    text = build_deterministic_brief(conn, now=now)
    assert "OVERDUE (0):" in text
    assert "(none)" in text


def test_full_brief_falls_back_gracefully_when_ai_unavailable(conn, monkeypatch):
    now = datetime.now(timezone.utc)

    def _raise(*args, **kwargs):
        raise AIAdapterError("AI is not configured (HERMES_BIN not resolved)")

    monkeypatch.setattr("ragra.ai.advisor.ask_for_priorities", _raise)

    text = build_full_brief(conn, now=now, hermes_bin=None)
    # The deterministic facts are still fully present even though the AI
    # call failed - the AI layer must never block the core brief.
    assert "OVERDUE (0):" in text
    assert "AI PRIORITY NOTES" not in text
    assert "AI priority notes unavailable" in text  # clear, user-facing explanation


def test_full_brief_appends_ai_notes_when_available(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("ragra.ai.advisor.ask_for_priorities", lambda *a, **kw: "Do the overdue thing first.")

    text = build_full_brief(conn, now=now, hermes_bin=None)
    assert "AI PRIORITY NOTES" in text
    assert "Do the overdue thing first." in text


def test_brief_module_has_no_top_level_ai_dependency():
    # ragra/brief.py must not import the AI package at module scope - only
    # build_full_brief() may reach for it, lazily, so the rest of this
    # module (and anything that merely imports it, like the web dashboard)
    # works even with the AI package unavailable.
    import ragra.brief

    assert not hasattr(ragra.brief, "ask_for_priorities")
    assert not hasattr(ragra.brief, "AIAdapterError")


def test_full_brief_degrades_gracefully_when_ai_package_is_unavailable(conn, monkeypatch):
    # Simulate the entire ragra.ai.advisor module being unimportable
    # (uninstalled/broken), not just unconfigured.
    monkeypatch.setitem(sys.modules, "ragra.ai.advisor", None)

    now = datetime.now(timezone.utc)
    text = build_full_brief(conn, now=now, hermes_bin=None)

    assert "OVERDUE (0):" in text
    assert "AI PRIORITY NOTES" not in text
    assert "AI priority notes unavailable" in text
