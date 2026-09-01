"""Central configuration for Ragra, loaded from the process environment.

Local paths (credential locations, the optional Hermes binary, etc.) are
supplied via environment variables loaded by the process entrypoint (see
cli.py) - never hardcoded here. Hermes is an optional personal notification
provider only (see ragra/adapters/notify.py); it is not required to locate
or load here, since core Classroom/Calendar/FAST/reminder functionality
never needs it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ragra.adapters.calendar import CalendarTokenPaths, default_calendar_token_path
from ragra.adapters.classroom import ClassroomTokenPaths, default_classroom_token_paths


def _ragra_home() -> Path:
    override = os.environ.get("RAGRA_HOME")
    if override:
        return Path(override)
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "ragra"
    return Path.home() / ".ragra"


def _default_google_client_dir() -> Path | None:
    """Best-effort default directory for the shared Google OAuth client
    registration and the Classroom token file. This is a file-location
    default only, not a code dependency on anything - it happens to match
    where these files already exist, so the already-granted authorization
    keeps working without a new consent flow. Fully overridable via
    RAGRA_GOOGLE_OAUTH_CLIENT_FILE / RAGRA_GOOGLE_CLASSROOM_TOKEN_DIR."""
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "hermes" / "google"
    return None


@dataclass(frozen=True)
class Config:
    ragra_home: Path
    db_path: Path
    hermes_bin: Path | None
    notify_target: str | None
    fast_student_id: str | None
    calendar_id: str
    calendar_paths: CalendarTokenPaths
    classroom_paths: ClassroomTokenPaths
    web_host: str
    web_port: int
    sheets_api_key: str | None
    fast_timetable_spreadsheet_id: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_use_ssl: bool
    email_from: str | None
    email_to: str | None
    web_base_url: str | None


def load_config() -> Config:
    home = _ragra_home()
    home.mkdir(parents=True, exist_ok=True)

    # Hermes is an optional personal notification provider only. HERMES_BIN
    # must be set explicitly if you want to use it - there is no
    # auto-discovery of a Hermes checkout, since core Ragra never needs one.
    hermes_bin_override = os.environ.get("HERMES_BIN")
    hermes_bin = Path(hermes_bin_override) if hermes_bin_override else None

    google_client_dir = _default_google_client_dir()

    oauth_client_override = os.environ.get("RAGRA_GOOGLE_OAUTH_CLIENT_FILE")
    if oauth_client_override:
        oauth_client_file = Path(oauth_client_override)
    else:
        oauth_client_file = (
            (google_client_dir / "credentials.json") if google_client_dir else home / "google_oauth_client.json"
        )

    classroom_token_dir_override = os.environ.get("RAGRA_GOOGLE_CLASSROOM_TOKEN_DIR")
    classroom_token_dir = Path(classroom_token_dir_override) if classroom_token_dir_override else google_client_dir
    classroom_paths = default_classroom_token_paths(classroom_token_dir or home)
    # The shared OAuth client registration always comes from oauth_client_file,
    # regardless of where the Classroom token itself lives.
    classroom_paths = ClassroomTokenPaths(
        credentials_file=oauth_client_file,
        token_file=classroom_paths.token_file,
        legacy_token_file=classroom_paths.legacy_token_file,
    )

    calendar_credential_override = os.environ.get("RAGRA_GOOGLE_CALENDAR_CREDENTIAL_FILE")
    calendar_credential_file = (
        Path(calendar_credential_override) if calendar_credential_override else default_calendar_token_path(home)
    )

    return Config(
        ragra_home=home,
        db_path=Path(os.environ.get("RAGRA_DB_PATH") or (home / "ragra.db")),
        hermes_bin=hermes_bin,
        notify_target=os.environ.get("RAGRA_NOTIFY_TARGET") or None,
        fast_student_id=os.environ.get("FAST_STUDENT_ID") or None,
        calendar_id=os.environ.get("RAGRA_CALENDAR_ID", "primary"),
        calendar_paths=CalendarTokenPaths(oauth_client_file, calendar_credential_file),
        classroom_paths=classroom_paths,
        web_host=os.environ.get("RAGRA_WEB_HOST", "127.0.0.1"),
        web_port=int(os.environ.get("RAGRA_WEB_PORT", "8731")),
        sheets_api_key=os.environ.get("RAGRA_SHEETS_API_KEY") or None,
        fast_timetable_spreadsheet_id=os.environ.get("RAGRA_FAST_TIMETABLE_SPREADSHEET_ID") or None,
        # Email is an optional provider only, same as Hermes - RAGRA_SMTP_HOST,
        # RAGRA_EMAIL_FROM, and RAGRA_EMAIL_TO must all be set for
        # _build_providers() (ragra/cli.py) to construct an EmailProvider.
        smtp_host=os.environ.get("RAGRA_SMTP_HOST") or None,
        smtp_port=int(os.environ.get("RAGRA_SMTP_PORT", "587")),
        smtp_username=os.environ.get("RAGRA_SMTP_USERNAME") or None,
        smtp_password=os.environ.get("RAGRA_SMTP_PASSWORD") or None,
        smtp_use_ssl=os.environ.get("RAGRA_SMTP_USE_SSL", "").strip().lower() in ("1", "true", "yes"),
        email_from=os.environ.get("RAGRA_EMAIL_FROM") or None,
        email_to=os.environ.get("RAGRA_EMAIL_TO") or None,
        # Optional deep link appended to email bodies (see EmailProvider) -
        # not required for email to work at all.
        web_base_url=os.environ.get("RAGRA_WEB_BASE_URL") or None,
    )
