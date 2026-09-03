"""Tests for the Missed-section dashboard fix: the main page shows only the
most recent MISSED tasks (a display limit, not a "historical" data
classification - see the app's MISSED_SECTION_PREVIEW_LIMIT), with the full,
unfiltered list still reachable at /missed.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragra.db import repo
from ragra.db.connection import connect
from ragra.web.app import MISSED_SECTION_PREVIEW_LIMIT, create_app

from tests.support import owner_id, sign_in


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "missed-web-test.db"
    conn = connect(db_path)
    course_id = repo.upsert_course(
        conn, external_id="course-1", name="Expository Writing-Lab", section="BCS-2G2",
        teacher="Dr. Smith", course_code=None, state="ACTIVE", user_id=owner_id(conn),
    )
    now = datetime.now(timezone.utc)
    # More missed tasks than the preview limit, spanning a wide age range -
    # mirrors the real "old assignments mixed with current ones" scenario.
    days_ago_values = [200, 150, 100, 60, 30, 10, 5, 1]
    for i, days_ago in enumerate(days_ago_values):
        deadline = (now - timedelta(days=days_ago)).isoformat()
        result = repo.upsert_task_from_source(
            conn, course_id=course_id, source_type="coursework", external_id=f"cw-{i}",
            title=f"Task due {days_ago}d ago", description=None, link=None, kind="ACTIONABLE",
            actual_deadline=deadline, source_published_at=repo.now_iso(), source_updated_at=repo.now_iso(), user_id=owner_id(conn),
        )
        repo.mark_missed(conn, task_id=result.task_id, user_id=owner_id(conn))
    conn.close()

    app = create_app(db_path)
    with TestClient(app) as c:
        sign_in(c, db_path)
        yield c


def test_main_dashboard_shows_only_the_preview_limit_of_missed_tasks(client):
    resp = client.get("/")
    missed_section = resp.text.split("<h2>Missed")[1].split("<h2>Due today</h2>")[0]

    # Most-recently-due ones (1, 5, 10, 30, 60 days ago) should be shown...
    assert "Task due 1d ago" in missed_section
    assert "Task due 5d ago" in missed_section
    assert "Task due 60d ago" in missed_section
    # ...but the oldest ones should NOT dominate the main view.
    assert "Task due 200d ago" not in missed_section
    assert "Task due 150d ago" not in missed_section
    assert missed_section.count('class="badge missed"') == MISSED_SECTION_PREVIEW_LIMIT


def test_main_dashboard_links_to_the_full_missed_list(client):
    resp = client.get("/")
    assert 'href="/missed"' in resp.text
    assert "See all 8 missed tasks" in resp.text


def test_missed_page_shows_every_missed_task_nothing_hidden(client):
    resp = client.get("/missed")
    assert resp.status_code == 200
    for days_ago in [200, 150, 100, 60, 30, 10, 5, 1]:
        assert f"Task due {days_ago}d ago" in resp.text
    assert resp.text.count('class="badge missed"') == 8


def test_missed_page_is_ordered_most_recent_first(client):
    resp = client.get("/missed")
    positions = {
        days_ago: resp.text.index(f"Task due {days_ago}d ago")
        for days_ago in [200, 150, 100, 60, 30, 10, 5, 1]
    }
    ordered = sorted(positions, key=lambda d: positions[d])
    assert ordered == [1, 5, 10, 30, 60, 100, 150, 200]
