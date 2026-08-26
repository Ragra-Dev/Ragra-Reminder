"""Central configuration for Ragra, loaded from the process environment.

Local paths (the Hermes checkout, the notification target, etc.) are
supplied via environment variables loaded by the process entrypoint (see
cli.py) - never hardcoded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ragra.adapters.calendar import CalendarTokenPaths, default_calendar_token_path


def _ragra_home() -> Path:
    override = os.environ.get("RAGRA_HOME")
    if override:
        return Path(override)
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "ragra"
    return Path.home() / ".ragra"


def _hermes_google_dir() -> Path | None:
    """Best-effort default for Hermes' Classroom credential directory,
    mirroring hermes_cli.classroom.oauth.default_google_dir()."""
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "hermes" / "google"
    return None


def _default_hermes_repo_path() -> Path | None:
    """Best-effort default for the Hermes Agent checkout itself, computed
    the same way hermes_cli.classroom.oauth derives its own default
    directory (LOCALAPPDATA/hermes/...) - no personal path hardcoded, and
    no env var required for the common case where Hermes lives in its
    standard install location."""
    local_appdata = os.getenv("LOCALAPPDATA")
    if not local_appdata:
        return None
    candidate = Path(local_appdata) / "hermes" / "hermes-agent"
    return candidate if (candidate / "hermes_cli").is_dir() else None


@dataclass(frozen=True)
class Config:
    ragra_home: Path
    db_path: Path
    hermes_repo_path: Path | None
    hermes_bin: Path | None
    notify_target: str | None
    fast_student_id: str | None
    calendar_id: str
    calendar_paths: CalendarTokenPaths
    web_host: str
    web_port: int
    sheets_api_key: str | None
    fast_timetable_spreadsheet_id: str | None


def load_config() -> Config:
    home = _ragra_home()
    home.mkdir(parents=True, exist_ok=True)

    hermes_repo_override = os.environ.get("HERMES_REPO_PATH")
    hermes_repo = Path(hermes_repo_override) if hermes_repo_override else _default_hermes_repo_path()

    hermes_bin_override = os.environ.get("HERMES_BIN")
    if hermes_bin_override:
        hermes_bin = Path(hermes_bin_override)
    elif hermes_repo:
        candidate = hermes_repo / "bin" / "hermes.exe"
        hermes_bin = candidate if candidate.exists() else None
    else:
        hermes_bin = None

    oauth_client_override = os.environ.get("RAGRA_GOOGLE_OAUTH_CLIENT_FILE")
    if oauth_client_override:
        oauth_client_file = Path(oauth_client_override)
    else:
        google_dir = _hermes_google_dir()
        oauth_client_file = (google_dir / "credentials.json") if google_dir else home / "google_oauth_client.json"

    calendar_credential_override = os.environ.get("RAGRA_GOOGLE_CALENDAR_CREDENTIAL_FILE")
    calendar_credential_file = (
        Path(calendar_credential_override) if calendar_credential_override else default_calendar_token_path(home)
    )

    return Config(
        ragra_home=home,
        db_path=Path(os.environ.get("RAGRA_DB_PATH") or (home / "ragra.db")),
        hermes_repo_path=hermes_repo,
        hermes_bin=hermes_bin,
        notify_target=os.environ.get("RAGRA_NOTIFY_TARGET") or None,
        fast_student_id=os.environ.get("FAST_STUDENT_ID") or None,
        calendar_id=os.environ.get("RAGRA_CALENDAR_ID", "primary"),
        calendar_paths=CalendarTokenPaths(oauth_client_file, calendar_credential_file),
        web_host=os.environ.get("RAGRA_WEB_HOST", "127.0.0.1"),
        web_port=int(os.environ.get("RAGRA_WEB_PORT", "8731")),
        sheets_api_key=os.environ.get("RAGRA_SHEETS_API_KEY") or None,
        fast_timetable_spreadsheet_id=os.environ.get("RAGRA_FAST_TIMETABLE_SPREADSHEET_ID") or None,
    )
