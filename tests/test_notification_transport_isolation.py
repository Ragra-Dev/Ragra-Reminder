"""The notification transport is isolated from Google authentication.

Ragra owns Google Classroom and Calendar. Hermes is an optional transport
that carries an already-composed message to WhatsApp. Those two concerns
must not touch, and this file is the executable statement of that boundary.

The tests exist because of a real incident. Obsolete Hermes cron automation
ran a script containing a stale hardcoded "Classroom API is currently down"
line, an LLM paraphrased it into "your Google sign-in expired", and it was
delivered to WhatsApp every morning while Ragra's Google credentials were
in fact healthy and refreshing normally. Nothing in Ragra produced that
message, but nothing in Ragra prevented it from being believed either.

So what is pinned here is: a notification provider performs no Google
operation, therefore no provider failure may ever be *described* as one.
Whatever breaks in delivery, it is not OAuth, not a token, and not
Classroom.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from ragra.adapters import notify
from ragra.adapters.notify import (
    HERMES_COMMAND_ERROR,
    HERMES_TRANSPORT_ERROR,
    NOT_CONFIGURED,
    HermesProvider,
    Notification,
    NotifyResult,
    send_to_all_providers,
)

# Any of these appearing in a transport failure would be the incident
# repeating itself: the notification layer claiming an authentication
# problem it is structurally incapable of observing.
GOOGLE_WORDS = (
    "google", "classroom", "oauth", "token", "credential", "refresh",
    "sign in", "signin", "expired", "invalid_grant", "scope", "consent",
    "authoriz", "authenticat",
)


def _assert_no_google_language(text: str) -> None:
    lowered = (text or "").lower()
    for word in GOOGLE_WORDS:
        assert word not in lowered, f"transport failure mentions {word!r}: {text!r}"


class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def hermes(tmp_path):
    binary = tmp_path / "hermes.exe"
    binary.write_text("", encoding="utf-8")
    return HermesProvider(hermes_bin=binary, target="whatsapp:test-channel")


# ---------------------------------------------------------------------------
# 1, 5, 6, 11 - delivery works with no Google credentials anywhere
# ---------------------------------------------------------------------------


def test_notification_is_delivered_with_no_google_credentials_configured(hermes, monkeypatch):
    """The whole point: Google being absent must be irrelevant to transport.

    Every Google-related environment variable is stripped, so if the
    provider consulted any of them this would fail rather than quietly
    succeed on ambient state.
    """
    for var in list(__import__("os").environ):
        if "GOOGLE" in var.upper() or "CLASSROOM" in var.upper():
            monkeypatch.delenv(var, raising=False)

    sent = {}

    def fake_run(cmd, **kwargs):
        sent["cmd"] = cmd
        return FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = hermes.send(Notification(text="Due in 3 days: CS1004: Assignment 1"))

    assert result.ok
    assert result.error is None


def test_a_google_outage_does_not_prevent_transport(hermes, monkeypatch):
    """A Classroom/Calendar API outage is upstream of notification content.

    Once Ragra has composed a notification, delivering it involves no
    Google call at all, so an outage cannot reach this path.
    """
    def exploding_google(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("the notification path attempted a Google call")

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: FakeCompleted(0))
    monkeypatch.setattr(notify, "_platform_transport_error", notify._platform_transport_error)

    result = hermes.send(Notification(text="New assignment: CS1004: Lab 04"))
    assert result.ok


def test_the_provider_needs_no_interactive_authorization(hermes, monkeypatch):
    """A fresh process sends without any browser or consent step.

    The provider is a frozen dataclass holding a path and a target; there
    is no login, no cached session to warm up, and nothing to prompt.
    """
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: FakeCompleted(0))
    fresh = HermesProvider(hermes_bin=hermes.hermes_bin, target=hermes.target)
    assert fresh.send(Notification(text="x")).ok


# ---------------------------------------------------------------------------
# 2, 12 - no Google/OAuth code is reachable from the notification module
# ---------------------------------------------------------------------------


def test_the_notification_module_imports_nothing_google(hermes):
    source = Path(inspect.getfile(notify)).read_text(encoding="utf-8")
    import_lines = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from ")) or line.lstrip().startswith(("import ", "from "))
    ]
    joined = " ".join(import_lines).lower()
    for forbidden in ("google", "oauth", "googleapiclient", "classroom", "ragra.adapters.classroom"):
        assert forbidden not in joined, f"notify.py imports {forbidden!r}"


def test_no_browser_or_oauth_flow_is_reachable_from_the_provider():
    """No consent flow, no local redirect server, no browser launch."""
    source = Path(inspect.getfile(notify)).read_text(encoding="utf-8")
    for forbidden in ("webbrowser", "InstalledAppFlow", "run_local_server", "authorization_url"):
        assert forbidden not in source, f"notify.py references {forbidden!r}"


# ---------------------------------------------------------------------------
# 3, 4 - the exact text is transported, and cannot be replaced
# ---------------------------------------------------------------------------


def test_the_exact_notification_text_is_passed_through_unchanged(hermes, monkeypatch):
    captured = {}

    def capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", capture)

    text = "Deadline changed: CS1004: Assignment 1 - now due 2026-09-20 18:59"
    hermes.send(Notification(text=text))

    # Passed as a single argv element: no shell, no interpolation, no
    # reformatting, and no opportunity for anything to rewrite it.
    assert text in captured["cmd"]
    assert captured["cmd"].index(text) == len(captured["cmd"]) - 2  # message, then --quiet


def test_transport_cannot_substitute_a_false_classroom_auth_message(hermes, monkeypatch):
    """Even a Hermes install that printed Classroom-auth chatter on success
    cannot turn that into the delivered content - the provider reports
    success or failure and never reads stdout back as the message."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: FakeCompleted(0, stdout="Google Classroom credentials expired, sign in again"),
    )
    result = hermes.send(Notification(text="New assignment: CS1004: Lab 04"))

    assert result.ok
    assert result.error is None  # the chatter is discarded, not surfaced


def test_no_llm_or_agent_layer_sits_in_the_provider():
    """`hermes send` is documented as "no LLM, no agent loop"; Ragra's side
    must not reintroduce one."""
    source = Path(inspect.getfile(notify)).read_text(encoding="utf-8").lower()
    for forbidden in ("openai", "anthropic", "llm", "completion(", "prompt="):
        assert forbidden not in source, f"notify.py references {forbidden!r}"


# ---------------------------------------------------------------------------
# 7 - failures are classified by the layer that actually failed
# ---------------------------------------------------------------------------


def test_a_missing_hermes_binary_is_a_hermes_transport_error(hermes, monkeypatch):
    def boom(*a, **k):
        raise OSError("[WinError 2] The system cannot find the file specified")

    monkeypatch.setattr(subprocess, "run", boom)
    result = hermes.send(Notification(text="x"))

    assert not result.ok
    assert result.code == HERMES_TRANSPORT_ERROR
    assert result.error.startswith(HERMES_TRANSPORT_ERROR)
    _assert_no_google_language(result.error)


def test_a_hang_is_a_hermes_transport_error(hermes, monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=30)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = hermes.send(Notification(text="x"))

    assert result.code == HERMES_TRANSPORT_ERROR
    _assert_no_google_language(result.error)


def test_a_usage_error_is_a_hermes_command_error(hermes, monkeypatch):
    """`hermes send` exits 2 for usage errors - that is Ragra calling the
    CLI wrongly, not the platform failing."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: FakeCompleted(2, stderr="hermes send: --to PLATFORM is required"),
    )
    result = hermes.send(Notification(text="x"))

    assert result.code == HERMES_COMMAND_ERROR
    _assert_no_google_language(result.error)


def test_a_delivery_failure_is_named_after_the_platform(hermes, monkeypatch):
    """Exit 1 is the messaging platform refusing or failing the send."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: FakeCompleted(1, stderr="whatsapp: session disconnected"),
    )
    result = hermes.send(Notification(text="x"))

    assert result.code == "WHATSAPP_TRANSPORT_ERROR"
    _assert_no_google_language(result.error)


def test_an_unconfigured_provider_says_so_rather_than_blaming_auth():
    result = notify.send_notification(hermes_bin=None, target=None, message="x")
    assert result.code == NOT_CONFIGURED
    _assert_no_google_language(result.error)


def test_a_transport_failure_never_reports_a_google_problem(hermes, monkeypatch):
    """The load-bearing guarantee, asserted across every failure mode.

    A provider performs no Google operation, so none of these codes may
    describe one - regardless of what the underlying tool happened to print.
    """
    failures = [
        lambda: (_ for _ in ()).throw(OSError("cannot find file")),
        lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("hermes", 30)),
    ]
    for raiser in failures:
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: raiser())
        result = hermes.send(Notification(text="x"))
        assert not result.ok
        assert "GOOGLE" not in result.code and "OAUTH" not in result.code
        _assert_no_google_language(result.code)

    # Even when the tool itself echoes authentication language back at us,
    # the classification stays at the layer that actually failed.
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: FakeCompleted(1, stderr="backend refused"),
    )
    result = hermes.send(Notification(text="x"))
    assert result.code == "WHATSAPP_TRANSPORT_ERROR"


# ---------------------------------------------------------------------------
# 8, 9, 10 - one logical notification, bounded retry, recorded delivery
# ---------------------------------------------------------------------------


def test_one_notification_produces_exactly_one_transport_invocation(hermes, monkeypatch):
    """A duplicate Hermes cron job cannot double a Ragra notification:
    Ragra's dispatch owns delivery, and one send is one subprocess call.
    Hermes cron jobs run their own scripts and never read Ragra's reminders
    table, so the two paths cannot amplify each other."""
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or FakeCompleted(0))

    hermes.send(Notification(text="New assignment: CS1004: Lab 04", reminder_id=42))
    assert len(calls) == 1


def test_each_provider_is_attempted_once_and_recorded_once(hermes, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: FakeCompleted(0))
    recorded = []

    delivered, errors = send_to_all_providers(
        [hermes], Notification(text="x", reminder_id=7),
        on_attempt=lambda name, result: recorded.append((name, result.ok)),
    )

    assert delivered is True
    assert errors == []
    assert recorded == [("HermesProvider", True)]


def test_a_recorder_that_raises_cannot_undo_a_successful_send(hermes, monkeypatch):
    """Bookkeeping failure must not turn a delivered message into a retry -
    that is how one notification becomes two WhatsApp messages."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: FakeCompleted(0))

    def exploding_recorder(name, result):
        raise RuntimeError("database is locked")

    delivered, errors = send_to_all_providers(
        [hermes], Notification(text="x"), on_attempt=exploding_recorder
    )
    assert delivered is True
    assert errors == []


def test_a_failed_send_is_reported_so_the_caller_can_retry(hermes, monkeypatch):
    """The provider itself holds no retry state - Ragra's dispatch owns the
    bounded retry budget, so there is exactly one retry queue in the system."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: FakeCompleted(1, stderr="disconnected"))

    delivered, errors = send_to_all_providers([hermes], Notification(text="x"))

    assert delivered is False
    assert len(errors) == 1
    assert errors[0].startswith("WHATSAPP_TRANSPORT_ERROR")
    assert not hasattr(hermes, "_queue") and not hasattr(hermes, "_retries")


def test_the_provider_is_stateless_between_sends(hermes, monkeypatch):
    """Frozen dataclass, no accumulated state: replaying a send cannot
    depend on what happened last time."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: FakeCompleted(0))
    before = repr(hermes)
    hermes.send(Notification(text="x"))
    assert repr(hermes) == before
    with pytest.raises(Exception):
        hermes.target = "whatsapp:somewhere-else"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The Hermes binary location is resolved dynamically, never hardcoded
# ---------------------------------------------------------------------------


def test_an_explicit_hermes_bin_wins(monkeypatch, tmp_path):
    from ragra.config import _resolve_hermes_bin

    pinned = tmp_path / "custom" / "hermes.exe"
    monkeypatch.setenv("HERMES_BIN", str(pinned))
    assert _resolve_hermes_bin() == pinned


def test_hermes_is_discovered_on_path_when_not_pinned(monkeypatch):
    import shutil as shutil_mod

    from ragra import config as config_mod

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(config_mod.shutil, "which",
                        lambda name: "/opt/hermes/bin/hermes" if name == "hermes" else None)
    assert config_mod._resolve_hermes_bin() == Path("/opt/hermes/bin/hermes")


def test_no_hermes_anywhere_is_a_supported_state(monkeypatch):
    from ragra import config as config_mod

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(config_mod.shutil, "which", lambda name: None)
    assert config_mod._resolve_hermes_bin() is None


def test_a_blank_hermes_bin_is_treated_as_unset(monkeypatch):
    """An empty env var must not become the path "", which would build a
    provider that fails on every single send."""
    from ragra import config as config_mod

    monkeypatch.setenv("HERMES_BIN", "   ")
    monkeypatch.setattr(config_mod.shutil, "which", lambda name: None)
    assert config_mod._resolve_hermes_bin() is None


def test_no_machine_specific_path_is_hardcoded_in_ragra_source():
    """The rule this file also enforces: no install path literals in source."""
    import subprocess as sp

    out = sp.run(
        ["git", "grep", "-niE", r"AppData.Local.hermes|/usr/local/bin/hermes|hermes-agent.bin", "--", "ragra/"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.stdout.strip() == "", f"hardcoded Hermes path in source:\n{out.stdout}"
