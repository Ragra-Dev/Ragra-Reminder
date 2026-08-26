"""Tests for ragra.adapters.classroom's tolerant, non-interactive credential
loading - the fix for Google permanently granting a subset of the requested
Classroom scopes on this project (missing coursework.me.readonly). These
never touch the network or a real credential; they use throwaway,
obviously-fake token files.
"""

import json
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
