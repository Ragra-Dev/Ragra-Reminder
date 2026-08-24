"""Tests for ragra.health - the self-alerting mechanism. Never touches a
real notification channel: send_notification is always monkeypatched.
"""

from ragra import health
from ragra.adapters.notify import NotifyResult


def test_success_keeps_streak_at_zero(conn):
    count = health.record_result(conn, component="classroom", success=True)
    assert count == 0
    row = conn.execute("SELECT * FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_success_at"] is not None


def test_failures_increment_the_streak(conn):
    health.record_result(conn, component="classroom", success=False, error="boom")
    health.record_result(conn, component="classroom", success=False, error="boom again")
    count = health.record_result(conn, component="classroom", success=False, error="boom a third time")

    assert count == 3
    row = conn.execute("SELECT * FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["consecutive_failures"] == 3
    assert row["last_error"] == "boom a third time"


def test_a_success_resets_a_prior_failure_streak(conn):
    health.record_result(conn, component="classroom", success=False, error="boom")
    health.record_result(conn, component="classroom", success=False, error="boom")
    count = health.record_result(conn, component="classroom", success=True)

    assert count == 0
    row = conn.execute("SELECT consecutive_failures FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["consecutive_failures"] == 0


def test_no_alert_below_threshold(conn, monkeypatch):
    sends = []
    monkeypatch.setattr(health, "send_notification", lambda **kw: (sends.append(kw), NotifyResult(ok=True))[1])

    for _ in range(health.FAILURE_ALERT_THRESHOLD - 1):
        health.record_result(conn, component="classroom", success=False, error="boom")

    alerted = health.check_and_alert(conn, hermes_bin="hermes.exe", notify_target="telegram")
    assert alerted == []
    assert sends == []


def test_alert_fires_exactly_once_at_threshold_and_not_again_for_the_same_streak(conn, monkeypatch):
    sends = []
    monkeypatch.setattr(health, "send_notification", lambda **kw: (sends.append(kw), NotifyResult(ok=True))[1])

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom")
    first = health.check_and_alert(conn, hermes_bin="hermes.exe", notify_target="telegram")

    # Keeps failing - must NOT alert again for the same ongoing streak
    # (no infinite notification loop).
    health.record_result(conn, component="classroom", success=False, error="boom")
    health.record_result(conn, component="classroom", success=False, error="boom")
    second = health.check_and_alert(conn, hermes_bin="hermes.exe", notify_target="telegram")

    assert first == ["classroom"]
    assert second == []
    assert len(sends) == 1


def test_recovery_then_new_failure_streak_alerts_again(conn, monkeypatch):
    monkeypatch.setattr(health, "send_notification", lambda **kw: NotifyResult(ok=True))

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom")
    first = health.check_and_alert(conn, hermes_bin="hermes.exe", notify_target="telegram")

    # Recovers - re-arms alerting.
    health.record_result(conn, component="classroom", success=True)

    # Fails again for a new, separate streak.
    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom again")
    second = health.check_and_alert(conn, hermes_bin="hermes.exe", notify_target="telegram")

    assert first == ["classroom"]
    assert second == ["classroom"]


def test_multiple_failing_components_are_combined_into_one_alert(conn, monkeypatch):
    sends = []
    monkeypatch.setattr(health, "send_notification", lambda **kw: (sends.append(kw), NotifyResult(ok=True))[1])

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="classroom broke")
        health.record_result(conn, component="calendar", success=False, error="calendar broke")

    alerted = health.check_and_alert(conn, hermes_bin="hermes.exe", notify_target="telegram")

    assert set(alerted) == {"classroom", "calendar"}
    assert len(sends) == 1  # one combined message, not two separate sends
    assert "classroom" in sends[0]["message"]
    assert "calendar" in sends[0]["message"]


def test_check_and_alert_does_nothing_when_not_configured(conn, monkeypatch):
    sends = []
    monkeypatch.setattr(health, "send_notification", lambda **kw: (sends.append(kw), NotifyResult(ok=True))[1])

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom")

    alerted = health.check_and_alert(conn, hermes_bin=None, notify_target=None)
    assert alerted == []
    assert sends == []  # never even attempted - nothing configured to send through


def test_undelivered_alert_is_retried_on_a_later_check(conn, monkeypatch):
    """If the alert send itself fails (e.g. Hermes is down too), it must
    not be marked as sent - the next check should try again."""
    monkeypatch.setattr(health, "send_notification", lambda **kw: NotifyResult(ok=False, error="hermes unreachable"))

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom")

    first = health.check_and_alert(conn, hermes_bin="hermes.exe", notify_target="telegram")
    assert first == []
    row = conn.execute("SELECT last_alert_sent_at FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["last_alert_sent_at"] is None
