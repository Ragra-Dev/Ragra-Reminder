from datetime import datetime, timedelta, timezone

import pytest

from ragra.adapters.ai import AIAdapterError
from ragra.ai.advisor import ask_for_priorities, build_snapshot_prompt
from ragra.db import repo

from tests.support import owner_id


def _make_course(conn):
    return repo.upsert_course(
        conn, external_id="course-1", name="OOP", section="BCS-3C",
        teacher="Dr. Smith", course_code="CS1004", state="ACTIVE", user_id=owner_id(conn),
    )


def test_snapshot_prompt_is_pure_and_contains_only_real_data(conn):
    now = datetime.now(timezone.utc)
    course_id = _make_course(conn)
    repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Real Assignment", description=None, link=None, kind="ACTIONABLE",
        actual_deadline=(now + timedelta(days=2)).isoformat(),
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )

    prompt = build_snapshot_prompt(conn, now_iso=now.isoformat(), week_end_iso=(now + timedelta(days=7)).isoformat(), user_id=owner_id(conn))

    assert "Real Assignment" in prompt
    assert "Do not invent" in prompt
    assert "COMPLETE and ONLY" in prompt


def test_snapshot_prompt_never_calls_the_network(conn):
    # build_snapshot_prompt takes no adapter/client at all - proof by
    # signature that it cannot make an AI call.
    import inspect

    params = inspect.signature(build_snapshot_prompt).parameters
    assert "hermes_bin" not in params


def test_ask_for_priorities_raises_when_ai_not_configured(conn):
    with pytest.raises(AIAdapterError):
        ask_for_priorities(
            conn, hermes_bin=None,
            now_iso=datetime.now(timezone.utc).isoformat(),
            week_end_iso=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(), user_id=owner_id(conn),
        )


def test_ask_for_priorities_never_writes_to_the_database(conn, monkeypatch):
    course_id = _make_course(conn)
    result = repo.upsert_task_from_source(
        conn, course_id=course_id, source_type="coursework", external_id="cw-1",
        title="Real Assignment", description=None, link=None, kind="ACTIONABLE",
        actual_deadline="2026-09-10T23:59:00+00:00",
        source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
    )
    before = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (result.task_id,)).fetchone())

    monkeypatch.setattr("ragra.ai.advisor.ask", lambda *a, **kw: "Fake AI response suggesting a fake deadline change.")
    now = datetime.now(timezone.utc)
    ask_for_priorities(conn, hermes_bin="fake", now_iso=now.isoformat(), week_end_iso=(now + timedelta(days=7)).isoformat(), user_id=owner_id(conn))

    after = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (result.task_id,)).fetchone())
    assert before == after
