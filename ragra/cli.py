"""Ragra CLI entrypoint.

Default behavior is always non-interactive: sync/reminders/serve/tick never
open a browser. Interactive Google authorization only ever happens via the
explicit `classroom-auth` / `calendar-auth` subcommands, and only when you
run them yourself.

`tick` is the one entrypoint meant to run unattended (Windows Task
Scheduler): it runs Classroom sync, Calendar sync, reminder dispatch, and
FAST timetable sync in sequence, with each step isolated so a transient
failure in one (e.g. a network blip talking to Google) never blocks the
others, logs to a rotating file since nothing is watching stdout when it
runs unattended, and tracks each step's health so a persisting failure
eventually raises one notification instead of failing silently forever
(see ragra/health.py). FAST timetable sync is independent of every other
step and of Hermes - it never requires the Hermes gateway to be running.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from ragra.adapters import calendar as calendar_adapter
from ragra.adapters import classroom as classroom_adapter
from ragra.adapters.notify import EmailProvider, HermesProvider, NotificationProvider
from ragra.config import Config, load_config
from ragra.db.connection import connect_closing
from ragra.reminders.dispatch import dispatch_due_reminders, preview_due_reminders
from ragra.sync.calendar_sync import sync_all_task_events
from ragra.sync.classroom_sync import sync_classroom


def _build_providers(config: Config) -> list[NotificationProvider]:
    """Builds the list of currently-configured notification providers.
    Hermes and email are both optional - Hermes included only when
    HERMES_BIN and RAGRA_NOTIFY_TARGET are set, email only when
    RAGRA_SMTP_HOST, RAGRA_EMAIL_FROM, and RAGRA_EMAIL_TO are all set. An
    empty list is normal and fully supported: core Ragra (Classroom/
    Calendar/FAST sync, the reminder engine) never requires any provider to
    be configured."""
    providers: list[NotificationProvider] = []
    if config.hermes_bin and config.notify_target:
        providers.append(HermesProvider(hermes_bin=config.hermes_bin, target=config.notify_target))
    if config.smtp_host and config.email_from and config.email_to:
        providers.append(
            EmailProvider(
                host=config.smtp_host,
                port=config.smtp_port,
                from_address=config.email_from,
                to_address=config.email_to,
                username=config.smtp_username,
                password=config.smtp_password,
                use_ssl=config.smtp_use_ssl,
                base_url=config.web_base_url,
            )
        )
    return providers


def cmd_classroom_status(args: argparse.Namespace) -> int:
    config = load_config()
    status = classroom_adapter.classroom_auth_status(config.classroom_paths)
    for key, value in status.items():
        print(f"{key}: {value}")
    print(
        "note: course teacher names are intentionally never looked up - that needs a "
        "roster scope Ragra deliberately doesn't request, and no current feature displays it."
    )
    return 0


def cmd_classroom_auth(args: argparse.Namespace) -> int:
    config = load_config()
    print("Opening a browser for Google Classroom authorization...")
    try:
        classroom_adapter.get_classroom_client(config.classroom_paths, interactive=True)
    except classroom_adapter.ClassroomAdapterError as exc:
        print("Authorization failed:", exc)
        return 1
    print("Classroom authorization succeeded and was saved for future silent refresh.")
    return 0


def cmd_timetable_sync(args: argparse.Namespace) -> int:
    """Manual, standalone invocation for on-demand runs/troubleshooting -
    `tick` also runs this same sync automatically every cycle."""
    from ragra.adapters.fast_timetable import FastTimetableAdapterError, FastTimetableClient, redact_api_key
    from ragra.sync.timetable_sync import TimetableSyncError, sync_timetable

    config = load_config()
    if not config.fast_timetable_spreadsheet_id:
        print("FAST timetable sync is not configured - set RAGRA_FAST_TIMETABLE_SPREADSHEET_ID. See .env.example.")
        return 1

    # RAGRA_SHEETS_API_KEY is optional: values are always read via the
    # public gviz endpoint (no credential needed); the key only enables
    # true tab-title enumeration instead of the name-guessing fallback.
    client = FastTimetableClient(config.fast_timetable_spreadsheet_id, config.sheets_api_key)
    if not client.has_metadata_access:
        print("(no RAGRA_SHEETS_API_KEY configured - using zero-credential weekday name discovery)")

    with connect_closing(config.db_path) as conn:
        try:
            summary = sync_timetable(conn, client, spreadsheet_id=config.fast_timetable_spreadsheet_id)
        except (TimetableSyncError, FastTimetableAdapterError) as exc:
            print(f"Timetable sync failed - existing data left untouched: {redact_api_key(str(exc))}")
            return 1

    print(
        f"Timetable sync: {summary.classes_found} class(es) found, "
        f"{summary.classes_created} new, {summary.classes_updated} updated, "
        f"{summary.classes_unchanged} unchanged, {summary.classes_cancelled} cancelled"
    )
    for issue in summary.unmatched_ambiguous:
        print(f"  ambiguous match: {issue}")
    return 0


def cmd_calendar_status(args: argparse.Namespace) -> int:
    config = load_config()
    status = calendar_adapter.calendar_auth_status(config.calendar_paths)
    for key, value in status.items():
        print(f"{key}: {value}")
    return 0


def cmd_calendar_auth(args: argparse.Namespace) -> int:
    config = load_config()
    print("Opening a browser for Google Calendar authorization (calendar.events scope only)...")
    try:
        calendar_adapter.ensure_calendar_credentials(config.calendar_paths, interactive=True)
    except calendar_adapter.CalendarAdapterError as exc:
        print("Authorization failed:", exc)
        return 1
    print("Calendar authorization succeeded and was saved for future silent refresh.")
    return 0


# ---------------------------------------------------------------------------
# Shared step runners - used by both the interactive commands (log=print)
# and `tick` (log=logger.info), each step isolated from the others. Each
# returns (exit_code, error_summary_or_None) so `tick` can feed a real
# error string into health tracking, not just a bare pass/fail bit.
# ---------------------------------------------------------------------------


def _run_classroom_sync(conn, config: Config, log) -> tuple[int, str | None]:
    try:
        client = classroom_adapter.get_classroom_client(config.classroom_paths, interactive=False)
    except classroom_adapter.ClassroomAdapterError as exc:
        msg = f"Classroom sync skipped - authorization required: {exc}. Run: ragra classroom-auth"
        log(msg)
        return 1, str(exc)
    except Exception as exc:  # noqa: BLE001 - a transient/network failure must not abort the tick
        log(f"Classroom sync failed unexpectedly: {exc}")
        return 1, str(exc)

    try:
        summary = sync_classroom(conn, client)
    except Exception as exc:  # noqa: BLE001
        log(f"Classroom sync failed unexpectedly: {exc}")
        return 1, str(exc)

    log(
        f"Classroom sync: {summary.courses_seen} course(s), "
        f"{summary.tasks_created} new task(s), {summary.tasks_updated} updated, "
        f"{summary.tasks_cancelled} cancelled, {len(summary.deadlines_changed)} deadline change(s)"
    )
    if summary.tasks_marked_missed:
        log(f"  ({summary.tasks_marked_missed} task(s) newly marked MISSED - past their actual deadline)")
    if summary.backlog_reminders_suppressed:
        log(f"  ({summary.backlog_reminders_suppressed} historical backlog reminder(s) suppressed - already overdue when discovered)")
    for change in summary.deadlines_changed:
        log(f"  deadline changed: {change['title']!r} {change['old_deadline']} -> {change['new_deadline']}")
    for error in summary.errors:
        log(f"  sync error: {error}")

    if summary.errors:
        return 1, "; ".join(summary.errors)
    return 0, None


def _run_calendar_sync(conn, config: Config, log) -> tuple[int, str | None]:
    try:
        calendar_credentials = calendar_adapter.ensure_calendar_credentials(config.calendar_paths, interactive=False)
    except calendar_adapter.CalendarAdapterError as exc:
        msg = f"Calendar sync skipped - authorization required: {exc}. Run: ragra calendar-auth"
        log(msg)
        return 1, str(exc)
    except Exception as exc:  # noqa: BLE001
        log(f"Calendar sync failed unexpectedly: {exc}")
        return 1, str(exc)

    try:
        calendar_client = calendar_adapter.GoogleCalendarClient(calendar_credentials)
        counts = sync_all_task_events(conn, calendar_client, calendar_id=config.calendar_id)
    except Exception as exc:  # noqa: BLE001 - a transient Calendar API failure must not abort the tick
        log(f"Calendar sync failed unexpectedly: {exc}")
        return 1, str(exc)

    log(f"Calendar sync: {counts}")
    return 0, None


def _run_reminders_dispatch(conn, config: Config, log) -> tuple[int, str | None]:
    now = datetime.now(timezone.utc).isoformat()
    providers = _build_providers(config)
    try:
        summary = dispatch_due_reminders(conn, providers=providers, now=now)
    except Exception as exc:  # noqa: BLE001
        log(f"Reminder dispatch failed unexpectedly: {exc}")
        return 1, str(exc)

    log(
        f"Reminders: {summary.sent} sent, {summary.retrying} retrying, "
        f"{summary.permanently_failed} permanently failed, {summary.skipped_not_configured} skipped (not configured)"
    )
    if summary.skipped_not_configured and not providers:
        log(
            "  NOTE: no notification provider is configured (e.g. RAGRA_NOTIFY_TARGET and/or HERMES_BIN "
            "for the optional Hermes integration), so due reminders are not being delivered anywhere. "
            "This is safe (nothing invented, nothing lost - they stay PENDING) but not useful until "
            "configured. See .env.example."
        )
    for error in summary.errors:
        log(f"  reminder error: {error}")

    if summary.permanently_failed:
        return 1, "; ".join(summary.errors)
    return 0, None


def _run_class_reminders(conn, config: Config, log) -> tuple[int, str | None]:
    """Announce classes starting shortly. Runs after timetable sync so it
    always reasons about the freshest pattern, and computes occurrences on
    demand - nothing about a future class is stored (see
    ragra/timetable/schedule.py)."""
    from ragra.reminders.class_reminders import run_class_reminders

    providers = _build_providers(config)
    try:
        summary = run_class_reminders(conn, providers=providers, now=datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001
        log(f"Class reminders failed unexpectedly: {exc}")
        return 1, str(exc)

    log(
        f"Class reminders: {summary.scheduled} scheduled, {summary.sent} sent, "
        f"{summary.retrying} retrying, {summary.expired} expired, "
        f"{summary.skipped_not_configured} skipped (not configured)"
    )
    for error in summary.errors:
        log(f"  class reminder error: {error}")
    return 0, None


def _run_timetable_sync(conn, config: Config, log) -> tuple[int, str | None]:
    if not config.fast_timetable_spreadsheet_id:
        log("Timetable sync skipped - RAGRA_FAST_TIMETABLE_SPREADSHEET_ID not set. See .env.example.")
        return 0, None

    from ragra.adapters.fast_timetable import FastTimetableAdapterError, FastTimetableClient, redact_api_key
    from ragra.sync.timetable_sync import TimetableSyncError, sync_timetable

    client = FastTimetableClient(config.fast_timetable_spreadsheet_id, config.sheets_api_key)
    try:
        summary = sync_timetable(conn, client, spreadsheet_id=config.fast_timetable_spreadsheet_id)
    except (TimetableSyncError, FastTimetableAdapterError) as exc:
        # Defense in depth: the adapter already redacts the key at its own
        # raise sites, but this also sanitizes here so nothing this
        # function ever logs can carry it, regardless of where an
        # exception's message originated.
        safe_message = redact_api_key(str(exc))
        log(f"Timetable sync failed - existing data left untouched: {safe_message}")
        return 1, safe_message
    except Exception as exc:  # noqa: BLE001 - a transient failure must not abort the tick
        safe_message = redact_api_key(str(exc))
        log(f"Timetable sync failed unexpectedly: {safe_message}")
        return 1, safe_message

    log(
        f"Timetable sync: {summary.classes_found} class(es) found, "
        f"{summary.classes_created} new, {summary.classes_updated} updated, "
        f"{summary.classes_unchanged} unchanged, {summary.classes_cancelled} cancelled"
    )
    for issue in summary.unmatched_ambiguous:
        log(f"  ambiguous match: {issue}")

    return 0, None


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config()
    with connect_closing(config.db_path) as conn:
        classroom_rc, _ = _run_classroom_sync(conn, config, print)
        calendar_rc, _ = _run_calendar_sync(conn, config, print)
    return classroom_rc or calendar_rc


def cmd_reminders(args: argparse.Namespace) -> int:
    config = load_config()
    now = datetime.now(timezone.utc).isoformat()
    with connect_closing(config.db_path) as conn:
        if args.dry_run:
            previews = preview_due_reminders(conn, now=now)
            if not previews:
                print("No reminders are due right now.")
                return 0
            print(f"{len(previews)} reminder(s) would be sent (dry run - nothing sent, nothing changed):")
            for p in previews:
                print(f"  [{p['reminder_type']}] due {p['scheduled_for']}: {p['message']!r}")
            return 0

        if not _build_providers(config):
            print(
                "No notification provider is configured (e.g. RAGRA_NOTIFY_TARGET and/or HERMES_BIN "
                "for the optional Hermes integration) - reminders will stay PENDING rather than being "
                "sent nowhere. See .env.example. (Use --dry-run to preview what would be sent once "
                "configured.)"
            )

        rc, _ = _run_reminders_dispatch(conn, config, print)
        return rc


def cmd_tick(args: argparse.Namespace) -> int:
    """The one entrypoint meant to run unattended (Windows Task Scheduler).
    Each step is isolated: Classroom sync, Calendar sync, reminder
    dispatch, and FAST timetable sync each catch their own failures and
    continue, so e.g. Google being briefly unreachable never prevents
    already-synced reminders from still being dispatched, and a FAST
    source hiccup never touches the other three. Logs to
    RAGRA_HOME/logs/ragra.log. Tracks each step's health (ragra/health.py)
    and sends at most one notification if a component has been failing for
    FAILURE_ALERT_THRESHOLD consecutive ticks - re-armed automatically the
    next time that component succeeds."""
    from ragra import health
    from ragra.db import repo
    from ragra.logging_setup import configure_logging

    config = load_config()
    logger = configure_logging(config.ragra_home)
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info("tick start")

    # Structured, short-retention diagnostics (separate table from the text
    # log): captures each stage's own summary line without changing what
    # any runner logs or returns - see ragra/db/repo.py's tick_sessions.
    stage_results: dict[str, str | None] = {
        "classroom": None, "calendar": None, "reminders": None, "timetable": None,
        "class_reminders": None,
    }
    tick_errors: list[str] = []

    def _capturing_log(component: str):
        def log(message: str) -> None:
            stage_results[component] = message if stage_results[component] is None else (
                stage_results[component] + "\n" + message
            )
            logger.info(message)

        return log

    exit_code = 0
    try:
        with connect_closing(config.db_path) as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            repo.purge_old_tick_sessions(conn, older_than_iso=cutoff)

            for component, runner in (
                ("classroom", _run_classroom_sync),
                ("calendar", _run_calendar_sync),
                ("reminders", _run_reminders_dispatch),
                ("timetable", _run_timetable_sync),
                # After timetable sync, so it always sees the freshest pattern.
                ("class_reminders", _run_class_reminders),
            ):
                rc, error = runner(conn, config, _capturing_log(component))
                health.record_result(conn, component=component, success=(rc == 0), error=error)
                if rc:
                    exit_code = 1
                    if error:
                        tick_errors.append(f"{component}: {error}")

            alerted = health.check_and_alert(conn, providers=_build_providers(config))
            if alerted:
                logger.info("health alert sent for: %s", ", ".join(alerted))

            repo.record_tick_session(
                conn,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=time.monotonic() - started,
                exit_code=exit_code,
                classroom_result=stage_results["classroom"],
                calendar_result=stage_results["calendar"],
                reminders_result=stage_results["reminders"],
                timetable_result=stage_results["timetable"],
                class_reminders_result=stage_results["class_reminders"],
                error="; ".join(tick_errors) if tick_errors else None,
            )
    except Exception as exc:  # noqa: BLE001 - last-resort guard; a tick must never leave a hung/corrupt process
        logger.error("tick failed unexpectedly: %s", exc)
        exit_code = 1
        try:
            with connect_closing(config.db_path) as conn:
                repo.record_tick_session(
                    conn,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    duration_seconds=time.monotonic() - started,
                    exit_code=exit_code,
                    classroom_result=stage_results["classroom"],
                    calendar_result=stage_results["calendar"],
                    reminders_result=stage_results["reminders"],
                    timetable_result=stage_results["timetable"],
                    class_reminders_result=stage_results["class_reminders"],
                    error=f"tick failed unexpectedly: {exc}",
                )
        except Exception:  # noqa: BLE001 - recording the diagnostic must never mask the real failure
            pass

    logger.info("tick end (%.1fs, exit=%d)", time.monotonic() - started, exit_code)
    return exit_code


def cmd_brief(args: argparse.Namespace) -> int:
    from ragra.brief import build_deterministic_brief, build_full_brief

    config = load_config()
    now = datetime.now(timezone.utc)
    with connect_closing(config.db_path) as conn:
        if args.ai:
            print(build_full_brief(conn, now=now, hermes_bin=config.hermes_bin))
        else:
            print(build_deterministic_brief(conn, now=now))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Optional AI feature entrypoint - never called from `tick` or any core
    sync/reminder path. Both the import of the AI package and the call
    itself are covered so a missing/unconfigured AI feature fails with a
    clear message instead of a traceback."""
    config = load_config()
    now = datetime.now(timezone.utc)
    week_end = now + timedelta(days=7)
    with connect_closing(config.db_path) as conn:
        try:
            from ragra.adapters.ai import AIAdapterError
            from ragra.ai.advisor import ask_for_priorities

            result = ask_for_priorities(
                conn, hermes_bin=config.hermes_bin, now_iso=now.isoformat(), week_end_iso=week_end.isoformat()
            )
        except ImportError as exc:
            print("AI advisor is not available:", exc)
            return 1
        except AIAdapterError as exc:
            print("AI advisory unavailable:", exc)
            return 1
    print(result)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    config = load_config()
    from ragra.web.app import create_app

    app = create_app(config.db_path)
    uvicorn.run(app, host=config.web_host, port=config.web_port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ragra")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("classroom-status", help="Show Classroom credential status (no browser, no secrets printed)").set_defaults(func=cmd_classroom_status)
    sub.add_parser("classroom-auth", help="Interactively authorize Classroom access (opens a browser)").set_defaults(func=cmd_classroom_auth)
    sub.add_parser("calendar-status", help="Show Calendar credential status (no browser, no secrets printed)").set_defaults(func=cmd_calendar_status)
    sub.add_parser("calendar-auth", help="Interactively authorize Ragra's Calendar access (opens a browser)").set_defaults(func=cmd_calendar_auth)
    sub.add_parser("sync", help="Sync Classroom -> database -> Calendar (never opens a browser)").set_defaults(func=cmd_sync)
    reminders_parser = sub.add_parser(
        "reminders", help="Dispatch any due reminders through the configured notification provider(s)"
    )
    reminders_parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be sent without sending anything or changing reminder state",
    )
    reminders_parser.set_defaults(func=cmd_reminders)
    sub.add_parser("serve", help="Run the Ragra dashboard").set_defaults(func=cmd_serve)
    sub.add_parser(
        "tick",
        help="Run sync + reminder dispatch once, logging to a file - the unattended/scheduled-task entrypoint",
    ).set_defaults(func=cmd_tick)

    brief_parser = sub.add_parser("brief", help="Print the deterministic daily academic brief")
    brief_parser.add_argument("--ai", action="store_true", help="Append an AI priority narrative (advisory only)")
    brief_parser.set_defaults(func=cmd_brief)

    sub.add_parser(
        "plan", help="Ask the AI advisor for a prioritized plan from current deterministic data (advisory only)"
    ).set_defaults(func=cmd_plan)

    sub.add_parser(
        "timetable-sync",
        help="Sync the FAST timetable (public spreadsheet, no OAuth) - runs automatically in `tick` every 15 minutes",
    ).set_defaults(func=cmd_timetable_sync)

    return parser


def main() -> None:
    # Reminder messages can contain non-ASCII characters (e.g. the FINAL_1H
    # warning emoji); the default Windows console codepage (cp1252) can't
    # encode them and would crash a plain print(). UTF-8 with a safe
    # fallback keeps output on any platform.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
