"""Tests for ragra.health - the self-alerting mechanism. Never touches a
real notification channel: a FakeProvider test double stands in for
whatever provider(s) would actually be configured.
"""

from dataclasses import dataclass, field

from ragra import health
from ragra.adapters.notify import Notification, NotifyResult

from tests.support import owner_id


@dataclass
class FakeProvider:
    result: NotifyResult
    calls: list[str] = field(default_factory=list)

    def send(self, notification: Notification) -> NotifyResult:
        self.calls.append(notification.text)
        return self.result


def test_success_keeps_streak_at_zero(conn):
    count = health.record_result(conn, component="classroom", success=True, user_id=owner_id(conn))
    assert count == 0
    row = conn.execute("SELECT * FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_success_at"] is not None


def test_failures_increment_the_streak(conn):
    health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))
    health.record_result(conn, component="classroom", success=False, error="boom again", user_id=owner_id(conn))
    count = health.record_result(conn, component="classroom", success=False, error="boom a third time", user_id=owner_id(conn))

    assert count == 3
    row = conn.execute("SELECT * FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["consecutive_failures"] == 3
    assert row["last_error"] == "boom a third time"


def test_a_success_resets_a_prior_failure_streak(conn):
    health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))
    health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))
    count = health.record_result(conn, component="classroom", success=True, user_id=owner_id(conn))

    assert count == 0
    row = conn.execute("SELECT consecutive_failures FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["consecutive_failures"] == 0


def test_no_alert_below_threshold(conn):
    provider = FakeProvider(NotifyResult(ok=True))

    for _ in range(health.FAILURE_ALERT_THRESHOLD - 1):
        health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))

    alerted = health.check_and_alert(conn, providers=[provider], user_id=owner_id(conn))
    assert alerted == []
    assert provider.calls == []


def test_alert_fires_exactly_once_at_threshold_and_not_again_for_the_same_streak(conn):
    provider = FakeProvider(NotifyResult(ok=True))

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))
    first = health.check_and_alert(conn, providers=[provider], user_id=owner_id(conn))

    # Keeps failing - must NOT alert again for the same ongoing streak
    # (no infinite notification loop).
    health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))
    health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))
    second = health.check_and_alert(conn, providers=[provider], user_id=owner_id(conn))

    assert first == ["classroom"]
    assert second == []
    assert len(provider.calls) == 1


def test_recovery_then_new_failure_streak_alerts_again(conn):
    provider = FakeProvider(NotifyResult(ok=True))

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))
    first = health.check_and_alert(conn, providers=[provider], user_id=owner_id(conn))

    # Recovers - re-arms alerting.
    health.record_result(conn, component="classroom", success=True, user_id=owner_id(conn))

    # Fails again for a new, separate streak.
    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom again", user_id=owner_id(conn))
    second = health.check_and_alert(conn, providers=[provider], user_id=owner_id(conn))

    assert first == ["classroom"]
    assert second == ["classroom"]


def test_multiple_failing_components_are_combined_into_one_alert(conn):
    provider = FakeProvider(NotifyResult(ok=True))

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="classroom broke", user_id=owner_id(conn))
        health.record_result(conn, component="calendar", success=False, error="calendar broke", user_id=owner_id(conn))

    alerted = health.check_and_alert(conn, providers=[provider], user_id=owner_id(conn))

    assert set(alerted) == {"classroom", "calendar"}
    assert len(provider.calls) == 1  # one combined message, not two separate sends
    assert "classroom" in provider.calls[0]
    assert "calendar" in provider.calls[0]


def test_check_and_alert_does_nothing_when_not_configured(conn):
    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))

    alerted = health.check_and_alert(conn, providers=[], user_id=owner_id(conn))
    assert alerted == []  # never even attempted - nothing configured to send through


def test_undelivered_alert_is_retried_on_a_later_check(conn):
    """If the alert send fails through every configured provider (e.g.
    Hermes is down too), it must not be marked as sent - the next check
    should try again."""
    provider = FakeProvider(NotifyResult(ok=False, error="hermes unreachable"))

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))

    first = health.check_and_alert(conn, providers=[provider], user_id=owner_id(conn))
    assert first == []
    row = conn.execute("SELECT last_alert_sent_at FROM pipeline_health WHERE component = 'classroom'").fetchone()
    assert row["last_alert_sent_at"] is None


def test_alert_delivers_via_any_successful_provider_when_others_fail(conn):
    failing = FakeProvider(NotifyResult(ok=False, error="channel A down"))
    succeeding = FakeProvider(NotifyResult(ok=True))

    for _ in range(health.FAILURE_ALERT_THRESHOLD):
        health.record_result(conn, component="classroom", success=False, error="boom", user_id=owner_id(conn))

    alerted = health.check_and_alert(conn, providers=[failing, succeeding], user_id=owner_id(conn))

    assert alerted == ["classroom"]
    assert len(failing.calls) == 1  # attempted, not skipped
    assert len(succeeding.calls) == 1
