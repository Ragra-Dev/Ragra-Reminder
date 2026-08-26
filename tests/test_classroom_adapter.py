"""Tests for ragra.adapters.classroom's tolerant, non-interactive credential
loading - the fix for Google permanently granting a subset of the requested
Classroom scopes on this project (missing coursework.me.readonly). These
never touch the network or a real credential; they use throwaway,
obviously-fake token files.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from ragra.adapters.classroom import ClassroomAdapterError, classroom_auth_status


class FakeOAuthPaths:
    def __init__(self, token_file, legacy_token_file, credentials_file):
        self.token_file = token_file
        self.legacy_token_file = legacy_token_file
        self.credentials_file = credentials_file


def _write_fake_token(path, *, scopes, expired=False):
    expiry = datetime.now(timezone.utc) + (timedelta(hours=-1) if expired else timedelta(hours=1))
    path.write_text(
        json.dumps(
            {
                "token": "fake-access-token-value-not-real",
                "refresh_token": "fake-refresh-token-value-not-real",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "fake-client-id-not-real",
                "client_secret": "fake-client-secret-value-not-real",
                "scopes": scopes,
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ),
        encoding="utf-8",
    )


REDUCED_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
]


def test_status_reports_missing_credential_without_raising(tmp_path):
    paths = FakeOAuthPaths(
        token_file=tmp_path / "token_classroom.json",
        legacy_token_file=tmp_path / "token.json",
        credentials_file=tmp_path / "credentials.json",
    )
    status = classroom_auth_status(paths)
    assert status["token_present"] is False
    assert status["usable"] is False


def test_status_reports_reduced_but_present_scopes_as_usable(tmp_path):
    token_file = tmp_path / "token_classroom.json"
    _write_fake_token(token_file, scopes=REDUCED_SCOPES)
    paths = FakeOAuthPaths(
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
        credentials_file=tmp_path / "credentials.json",
    )
    status = classroom_auth_status(paths)
    assert status["token_present"] is True
    assert status["usable"] is True
    assert status["granted_scopes"] == REDUCED_SCOPES
    # The known-unavailable scope correctly does not appear - and that is
    # not, by itself, treated as "unusable".
    assert "https://www.googleapis.com/auth/classroom.coursework.me.readonly" not in status["granted_scopes"]


def test_load_and_refresh_accepts_a_valid_reduced_scope_credential(tmp_path):
    from ragra.adapters.classroom import _load_and_refresh

    token_file = tmp_path / "token_classroom.json"
    _write_fake_token(token_file, scopes=REDUCED_SCOPES, expired=False)
    paths = FakeOAuthPaths(
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
        credentials_file=tmp_path / "credentials.json",
    )

    creds = _load_and_refresh(paths)
    assert set(creds.scopes) == set(REDUCED_SCOPES)
    assert creds.valid is True


def test_load_and_refresh_raises_cleanly_when_no_token_exists(tmp_path):
    from ragra.adapters.classroom import _load_and_refresh

    paths = FakeOAuthPaths(
        token_file=tmp_path / "token_classroom.json",
        legacy_token_file=tmp_path / "token.json",
        credentials_file=tmp_path / "credentials.json",
    )
    with pytest.raises(ClassroomAdapterError):
        _load_and_refresh(paths)


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def _google_credentials_logger():
    """Importing ragra.adapters.classroom (already done at module import
    time above) registers the scope-warning filter on this shared,
    third-party logger once per process - these tests attach their own
    handler to observe what actually gets through it."""
    logger = logging.getLogger("google.oauth2.credentials")
    handler = _RecordingHandler()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    yield logger, handler
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


def test_known_benign_classroom_scope_refresh_warning_is_suppressed(_google_credentials_logger):
    logger, handler = _google_credentials_logger
    logger.warning(
        "Not all requested scopes were granted by the authorization server, "
        "missing scopes https://www.googleapis.com/auth/classroom.coursework.me.readonly."
    )
    assert handler.records == []


def test_a_different_scope_warning_on_the_same_logger_still_surfaces(_google_credentials_logger):
    # Defense against over-broad suppression: a genuinely different missing
    # scope (e.g. Calendar's) must never be silently dropped just because it
    # shares the same logger and the same generic message prefix.
    logger, handler = _google_credentials_logger
    logger.warning(
        "Not all requested scopes were granted by the authorization server, "
        "missing scopes https://www.googleapis.com/auth/calendar.events."
    )
    assert len(handler.records) == 1
    assert "calendar.events" in handler.records[0].getMessage()


def test_an_unrelated_error_on_the_same_logger_still_surfaces(_google_credentials_logger):
    # A genuine, real auth failure must never be silently dropped.
    logger, handler = _google_credentials_logger
    logger.error("Some completely different real auth error.")
    assert len(handler.records) == 1
    assert handler.records[0].levelno == logging.ERROR


def test_load_and_refresh_raises_cleanly_on_unrefreshable_expired_credential(tmp_path):
    from ragra.adapters.classroom import _load_and_refresh

    token_file = tmp_path / "token_classroom.json"
    expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    token_file.write_text(
        json.dumps(
            {
                "token": "fake-access-token-value-not-real",
                "refresh_token": None,
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "fake-client-id-not-real",
                "client_secret": "fake-client-secret-value-not-real",
                "scopes": REDUCED_SCOPES,
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ),
        encoding="utf-8",
    )
    paths = FakeOAuthPaths(
        token_file=token_file,
        legacy_token_file=tmp_path / "token.json",
        credentials_file=tmp_path / "credentials.json",
    )
    with pytest.raises(ClassroomAdapterError):
        _load_and_refresh(paths)
