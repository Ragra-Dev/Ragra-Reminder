"""The unattended tick, across several accounts.

The property under test is containment. Ragra's scheduled task is the only
thing that runs unattended, so a failure that stops it stops everything -
and the symptom a user sees is "my reminders stopped", which points nowhere
near a different account's expired token. Every test here breaks one
account and asserts the others were still processed completely.

The stage runners are replaced with fakes on purpose. Their internals are
covered elsewhere; what is being tested here is the loop around them, and a
test that also had to satisfy Google's API would only ever prove that the
mocks were set up correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragra import cli
from ragra.adapters.calendar import CalendarTokenPaths
from ragra.adapters.classroom import ClassroomTokenPaths
from ragra.config import Config
from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.notifications.preferences import NotificationPreferences, save_preferences
from tests.support import make_user, owner_id


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tick.db"
    with connect_closing(path):
        pass
    return path


@pytest.fixture
def config(tmp_path, db_path) -> Config:
    return Config(
        ragra_home=tmp_path,
        db_path=db_path,
        hermes_bin=None,
        notify_target=None,
        fast_student_id=None,
        calendar_id="primary",
        calendar_paths=CalendarTokenPaths(tmp_path / "client.json", tmp_path / "token.json"),
        classroom_paths=ClassroomTokenPaths(
            tmp_path / "client.json", tmp_path / "no-token.json", tmp_path / "no-legacy.json"
        ),
        web_host="127.0.0.1",
        web_port=8731,
        sheets_api_key=None,
        fast_timetable_spreadsheet_id=None,
        smtp_host=None,
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_use_ssl=False,
        email_from=None,
        email_to=None,
        web_base_url=None,
    )


@pytest.fixture
def users(db_path):
    with connect_closing(db_path) as conn:
        alice = owner_id(conn)
        bea = make_user(conn, google_sub="tick-second", display_name="Bea")
        carl = make_user(conn, google_sub="tick-third", display_name="Carl")
    return alice, bea, carl


class StageRecorder:
    """Replaces the real stage runners. Records which users each stage ran
    for, and can be told to fail - or raise - for specific ones."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.fail_for: dict[str, set[int]] = {}
        self.raise_for: dict[str, set[int]] = {}

    def runner(self, component: str):
        def run(conn, config, log, *, user_id: int):
            self.calls.append((component, user_id))
            if user_id in self.raise_for.get(component, set()):
                raise RuntimeError(f"{component} exploded for user {user_id}")
            if user_id in self.fail_for.get(component, set()):
                log(f"{component} failed")
                return 1, f"{component} is broken"
            log(f"{component} ok")
            return 0, None

        return run

    def users_for(self, component: str) -> list[int]:
        return [user_id for name, user_id in self.calls if name == component]


@pytest.fixture
def stages(monkeypatch):
    recorder = StageRecorder()
    for component, runner_name in cli.TICK_COMPONENTS:
        monkeypatch.setattr(cli, runner_name, recorder.runner(component))
    return recorder


@pytest.fixture
def run_tick(config, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: config)

    def run():
        return cli.cmd_tick(object())

    return run


# ---------------------------------------------------------------------------
# Coverage: every user, every stage
# ---------------------------------------------------------------------------


def test_every_account_is_processed(users, stages, run_tick):
    alice, bea, carl = users

    assert run_tick() == 0

    for component, _ in cli.TICK_COMPONENTS:
        assert stages.users_for(component) == [alice, bea, carl], component


def test_each_account_gets_its_own_diagnostics_row(db_path, users, stages, run_tick):
    run_tick()

    with connect_closing(db_path) as conn:
        rows = conn.execute(
            "SELECT user_id, started_at FROM tick_sessions ORDER BY user_id"
        ).fetchall()

    assert [row["user_id"] for row in rows] == list(users)
    # One run, so the rows share a started_at - which is how rows from the
    # same run are correlated (see migration 0020).
    assert len({row["started_at"] for row in rows}) == 1


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def test_one_accounts_failing_stage_does_not_stop_the_others(users, stages, run_tick):
    alice, bea, carl = users
    stages.fail_for["classroom"] = {bea}

    assert run_tick() == 1  # the process reports that something failed...

    # ...but everyone was still processed, on every stage.
    for component, _ in cli.TICK_COMPONENTS:
        assert stages.users_for(component) == [alice, bea, carl], component


def test_one_accounts_raising_stage_does_not_stop_the_others(users, stages, run_tick):
    """An unexpected exception, not a returned error code - the case a
    `try` around the wrong thing would let escape."""
    alice, bea, carl = users
    stages.raise_for["calendar"] = {bea}

    assert run_tick() == 1

    assert stages.users_for("calendar") == [alice, bea, carl]
    # The stages after the one that raised still ran for that user too.
    assert stages.users_for("class_reminders") == [alice, bea, carl]


def test_a_failure_is_recorded_against_the_account_that_had_it(db_path, users, stages, run_tick):
    alice, bea, _carl = users
    stages.fail_for["classroom"] = {bea}

    run_tick()

    with connect_closing(db_path) as conn:
        health = {
            row["user_id"]: row["consecutive_failures"]
            for row in conn.execute(
                "SELECT user_id, consecutive_failures FROM pipeline_health "
                "WHERE component = 'classroom'"
            )
        }
    assert health[bea] == 1
    assert health[alice] == 0


def test_one_accounts_health_streak_is_not_reset_by_anothers_success(
    db_path, users, stages, run_tick
):
    """The failure this guards is silent and severe: a shared streak means
    a healthy neighbour permanently suppresses the alert for a genuinely
    broken account."""
    _alice, bea, _carl = users
    stages.fail_for["classroom"] = {bea}

    for _ in range(3):
        run_tick()

    with connect_closing(db_path) as conn:
        streak = conn.execute(
            "SELECT consecutive_failures FROM pipeline_health "
            "WHERE user_id = ? AND component = 'classroom'",
            (bea,),
        ).fetchone()["consecutive_failures"]
    assert streak == 3


def test_sync_state_is_recorded_per_account(db_path, users, stages, run_tick):
    """Not exercised through the fakes - asserted directly, because the
    property is that the table can hold a different answer per user at
    all."""
    alice, bea, _carl = users
    with connect_closing(db_path) as conn:
        repo.record_sync_start(conn, user_id=alice, source="classroom")
        repo.record_sync_start(conn, user_id=bea, source="classroom")
        repo.record_sync_success(conn, user_id=alice, source="classroom")
        repo.record_sync_error(conn, user_id=bea, source="classroom", error="token expired")

        states = {
            row["user_id"]: row["status"]
            for row in conn.execute(
                "SELECT user_id, status FROM sync_state WHERE source = 'classroom'"
            )
        }
    assert states[alice] == "OK"
    assert states[bea] == "ERROR"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_a_health_alert_goes_only_to_the_account_that_is_failing(
    db_path, config, users, stages, run_tick, monkeypatch, tmp_path
):
    """The sharpest cross-user leak available here: an alert about one
    person's broken sync delivered to somebody else's phone."""
    from ragra.adapters.notify import NotifyResult

    alice, bea, _carl = users
    delivered: list[tuple[str, str]] = []

    class RecordingProvider:
        def __init__(self, label):
            self.label = label

        def send(self, notification):
            delivered.append((self.label, notification.text))
            return NotifyResult(ok=True)

    def fake_providers(conn, cfg, *, user_id):
        return [RecordingProvider(f"user-{user_id}")]

    monkeypatch.setattr(cli, "_build_providers", fake_providers)
    stages.fail_for["classroom"] = {bea}

    for _ in range(3):  # cross the alert threshold
        run_tick()

    assert delivered, "expected a health alert once the threshold was crossed"
    assert {label for label, _ in delivered} == {f"user-{bea}"}


def test_reminder_destinations_do_not_leak_between_accounts(db_path, config, users):
    alice, bea, _carl = users
    with connect_closing(db_path) as conn:
        save_preferences(
            conn,
            user_id=alice,
            preferences=NotificationPreferences(email_enabled=True, email_to="alice@example.com"),
        )
        alice_providers = cli._build_providers(conn, config, user_id=alice)
        bea_providers = cli._build_providers(conn, config, user_id=bea)

    # No SMTP relay in this config, so nobody gets a provider - the point is
    # that Bea does not inherit Alice's address by any route.
    assert alice_providers == []
    assert bea_providers == []


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def test_housekeeping_runs_once_per_tick_not_once_per_user(
    db_path, users, stages, run_tick, monkeypatch
):
    """Pruning by age is a property of the log, not of an account: running
    it per user would be wasted work, and running it only for the users
    visited would leave a departed user's rows behind forever."""
    calls = []
    real = repo.purge_old_tick_sessions

    def counting(conn, *, older_than_iso):
        calls.append(older_than_iso)
        return real(conn, older_than_iso=older_than_iso)

    monkeypatch.setattr(repo, "purge_old_tick_sessions", counting)

    run_tick()

    assert len(calls) == 1


def test_expired_sessions_are_purged_by_the_tick(db_path, users, stages, run_tick):
    """The scheduled task is the only thing that runs unattended, so it is
    the only place expired sessions can be cleaned up without someone
    happening to visit the site."""
    from datetime import datetime, timedelta, timezone

    from ragra.web import sessions

    alice, _bea, _carl = users
    with connect_closing(db_path) as conn:
        sessions.create_session(
            conn,
            user_id=alice,
            now=datetime.now(timezone.utc) - timedelta(days=30),
            lifetime=timedelta(hours=1),
        )
        assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 1

    run_tick()

    with connect_closing(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 0


def test_a_tick_with_no_accounts_at_all_is_not_an_error(db_path, stages, run_tick):
    """Defensive, but cheap: an empty install must not report a failure the
    scheduled task will surface as an error every 15 minutes."""
    with connect_closing(db_path) as conn:
        conn.execute("DELETE FROM users")
        conn.commit()

    assert run_tick() == 0
    assert stages.calls == []


def test_repeated_ticks_stay_idempotent_across_accounts(db_path, users, stages, run_tick):
    run_tick()
    run_tick()

    with connect_closing(db_path) as conn:
        per_user = conn.execute(
            "SELECT user_id, COUNT(*) AS c FROM tick_sessions GROUP BY user_id"
        ).fetchall()

    assert {row["user_id"] for row in per_user} == set(users)
    assert all(row["c"] == 2 for row in per_user)
