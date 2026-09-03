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
from pathlib import Path

from dotenv import load_dotenv

from ragra.adapters import calendar as calendar_adapter
from ragra.adapters import classroom as classroom_adapter
from ragra.adapters.notify import NotificationProvider
from ragra.config import Config, load_config
from ragra.db.connection import connect_closing
from ragra.adapters.user_clients import UserCredentialsUnavailable, calendar_client_for, classroom_client_for
from ragra.notifications.preferences import providers_for
from ragra.reminders.dispatch import dispatch_due_reminders, preview_due_reminders
from ragra.sync.calendar_sync import sync_all_task_events
from ragra.sync.classroom_sync import sync_classroom


def _build_providers(conn, config: Config, *, user_id: int) -> list[NotificationProvider]:
    """The notification providers for one user.

    Destinations are per-user data and infrastructure is per-deployment
    (see ragra/notifications/preferences.py). There is deliberately no
    fallback to a globally configured recipient: that fallback is exactly
    how a second user's reminders would be delivered to the first.

    An empty list is normal and fully supported - core Ragra never requires
    any provider, and reminders stay PENDING rather than being sent
    somewhere they do not belong.
    """
    return providers_for(conn, config, user_id=user_id)


class NoCurrentUser(RuntimeError):
    """No acting user could be resolved. The CLI refuses to guess an owner
    for the same reason the web layer does - picking "the first user" is how
    one account's sync ends up writing into another's data."""


def acting_user_id(conn) -> int:
    """Which user this CLI invocation acts for.

    Every command resolves this once and threads it explicitly through the
    step runners, so no code path can reach the repository layer without
    naming an owner. `tick` currently runs it for the single pre-identity
    owner; P3-10 turns that into a loop over users, which changes this
    resolution point rather than any runner.
    """
    from ragra.db import repo

    user_id = repo.unlinked_user_id(conn)
    if user_id is None:
        raise NoCurrentUser(
            "no unambiguous user to act for; sign-in is required before this command can run"
        )
    return user_id


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
            summary = sync_timetable(
                conn, client,
                user_id=acting_user_id(conn),
                spreadsheet_id=config.fast_timetable_spreadsheet_id,
            )
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


def _run_classroom_sync(conn, config: Config, log, *, user_id: int) -> tuple[int, str | None]:
    try:
        client = classroom_client_for(conn, config, user_id=user_id)
    except UserCredentialsUnavailable as exc:
        # Not a failure of the run: an account that has not connected
        # Google yet is a normal state, and counting it as a failure would
        # feed a permanent alert streak into ragra/health.py for something
        # nobody intends to fix.
        log(f"Classroom sync skipped - authorization required: {exc}. Run: ragra classroom-auth")
        return 1, str(exc)
    except Exception as exc:  # noqa: BLE001 - a transient/network failure must not abort the tick
        log(f"Classroom sync failed unexpectedly: {exc}")
        return 1, str(exc)

    try:
        summary = sync_classroom(conn, client, user_id=user_id)
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


def _run_calendar_sync(conn, config: Config, log, *, user_id: int) -> tuple[int, str | None]:
    try:
        calendar_client = calendar_client_for(conn, config, user_id=user_id)
    except UserCredentialsUnavailable as exc:
        log(f"Calendar sync skipped - authorization required: {exc}. Run: ragra calendar-auth")
        return 1, str(exc)
    except Exception as exc:  # noqa: BLE001
        log(f"Calendar sync failed unexpectedly: {exc}")
        return 1, str(exc)

    try:
        counts = sync_all_task_events(
            conn, calendar_client, user_id=user_id, calendar_id=config.calendar_id
        )
    except Exception as exc:  # noqa: BLE001 - a transient Calendar API failure must not abort the tick
        log(f"Calendar sync failed unexpectedly: {exc}")
        return 1, str(exc)

    log(f"Calendar sync: {counts}")
    return 0, None


def _run_reminders_dispatch(conn, config: Config, log, *, user_id: int) -> tuple[int, str | None]:
    now = datetime.now(timezone.utc).isoformat()
    providers = _build_providers(conn, config, user_id=user_id)
    try:
        summary = dispatch_due_reminders(conn, user_id=user_id, providers=providers, now=now)
    except Exception as exc:  # noqa: BLE001
        log(f"Reminder dispatch failed unexpectedly: {exc}")
        return 1, str(exc)

    log(
        f"Reminders: {summary.sent} sent, {summary.retrying} retrying, "
        f"{summary.permanently_failed} permanently failed, {summary.skipped_not_configured} skipped (not configured)"
    )
    if summary.skipped_not_configured and not providers:
        log(
            "  NOTE: this account has no notification destination configured, so due reminders "
            "are not being delivered anywhere. This is safe (nothing invented, nothing lost - "
            "they stay PENDING) but not useful until configured. Set one with: "
            "ragra notify-set --email you@example.com"
        )
    for error in summary.errors:
        log(f"  reminder error: {error}")

    if summary.permanently_failed:
        return 1, "; ".join(summary.errors)
    return 0, None


def _run_class_reminders(conn, config: Config, log, *, user_id: int) -> tuple[int, str | None]:
    """Announce classes starting shortly. Runs after timetable sync so it
    always reasons about the freshest pattern, and computes occurrences on
    demand - nothing about a future class is stored (see
    ragra/timetable/schedule.py)."""
    from ragra.reminders.class_reminders import run_class_reminders

    providers = _build_providers(conn, config, user_id=user_id)
    try:
        summary = run_class_reminders(
            conn, user_id=user_id, providers=providers, now=datetime.now(timezone.utc)
        )
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


def _run_timetable_sync(conn, config: Config, log, *, user_id: int) -> tuple[int, str | None]:
    if not config.fast_timetable_spreadsheet_id:
        log("Timetable sync skipped - RAGRA_FAST_TIMETABLE_SPREADSHEET_ID not set. See .env.example.")
        return 0, None

    from ragra.adapters.fast_timetable import FastTimetableAdapterError, FastTimetableClient, redact_api_key
    from ragra.sync.timetable_sync import TimetableSyncError, sync_timetable

    client = FastTimetableClient(config.fast_timetable_spreadsheet_id, config.sheets_api_key)
    try:
        summary = sync_timetable(
            conn, client, user_id=user_id, spreadsheet_id=config.fast_timetable_spreadsheet_id
        )
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
        user_id = acting_user_id(conn)
        classroom_rc, _ = _run_classroom_sync(conn, config, print, user_id=user_id)
        calendar_rc, _ = _run_calendar_sync(conn, config, print, user_id=user_id)
    return classroom_rc or calendar_rc


def cmd_reminders(args: argparse.Namespace) -> int:
    config = load_config()
    now = datetime.now(timezone.utc).isoformat()
    with connect_closing(config.db_path) as conn:
        user_id = acting_user_id(conn)
        if args.dry_run:
            previews = preview_due_reminders(conn, user_id=user_id, now=now)
            if not previews:
                print("No reminders are due right now.")
                return 0
            print(f"{len(previews)} reminder(s) would be sent (dry run - nothing sent, nothing changed):")
            for p in previews:
                print(f"  [{p['reminder_type']}] due {p['scheduled_for']}: {p['message']!r}")
            return 0

        if not _build_providers(conn, config, user_id=user_id):
            print(
                "This account has no notification destination configured - reminders will stay "
                "PENDING rather than being sent nowhere. Set one with `ragra notify-set`, or use "
                "--dry-run to preview what would be sent once configured."
            )

        rc, _ = _run_reminders_dispatch(conn, config, print, user_id=user_id)
        return rc


TICK_COMPONENTS = (
    ("classroom", "_run_classroom_sync"),
    ("calendar", "_run_calendar_sync"),
    ("reminders", "_run_reminders_dispatch"),
    ("timetable", "_run_timetable_sync"),
    # After timetable sync, so it always sees the freshest pattern.
    ("class_reminders", "_run_class_reminders"),
)


def _tick_one_user(conn, config: Config, logger, *, user_id: int, started_at: str) -> tuple[int, list[str]]:
    """Run every stage for one account and record that account's
    diagnostics. Returns (exit_code, errors).

    Each stage is already isolated from the others; this function is the
    isolation boundary between *users*. Nothing it does can reach another
    account: every call below carries user_id, health and sync state are
    per-user rows (migrations 0018-0019), and the providers are built from
    this user's own preferences.
    """
    from ragra import health
    from ragra.db import repo

    stage_results: dict[str, str | None] = {name: None for name, _ in TICK_COMPONENTS}
    errors: list[str] = []
    started = time.monotonic()

    def _capturing_log(component: str):
        def log(message: str) -> None:
            stage_results[component] = message if stage_results[component] is None else (
                stage_results[component] + "\n" + message
            )
            logger.info("[user %s] %s", user_id, message)

        return log

    exit_code = 0
    for component, runner_name in TICK_COMPONENTS:
        runner = globals()[runner_name]
        try:
            rc, error = runner(conn, config, _capturing_log(component), user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - one stage must never abort the user
            rc, error = 1, str(exc)
            logger.error("[user %s] %s stage raised: %s", user_id, component, exc)

        health.record_result(
            conn, user_id=user_id, component=component, success=(rc == 0), error=error
        )
        if rc:
            exit_code = 1
            if error:
                errors.append(f"{component}: {error}")

    alerted = health.check_and_alert(
        conn, user_id=user_id, providers=_build_providers(conn, config, user_id=user_id)
    )
    if alerted:
        logger.info("[user %s] health alert sent for: %s", user_id, ", ".join(alerted))

    repo.record_tick_session(
        conn,
        user_id=user_id,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=time.monotonic() - started,
        exit_code=exit_code,
        classroom_result=stage_results["classroom"],
        calendar_result=stage_results["calendar"],
        reminders_result=stage_results["reminders"],
        timetable_result=stage_results["timetable"],
        class_reminders_result=stage_results["class_reminders"],
        error="; ".join(errors) if errors else None,
    )
    return exit_code, errors


def cmd_tick(args: argparse.Namespace) -> int:
    """The one entrypoint meant to run unattended (Windows Task Scheduler).

    Runs every stage for every user. Isolation works at two levels, and both
    matter for different reasons:

      - between stages, so Google being briefly unreachable never prevents
        already-synced reminders from being dispatched;
      - between users, so one account's expired token, malformed timetable,
        or unreachable notification provider cannot stop any other account
        from being processed at all. Without that, a single broken account
        would silently take the whole system down for everyone, and the
        symptom - "reminders stopped" - would point nowhere near the cause.

    Logs to RAGRA_HOME/logs/ragra.log, prefixed by user. Tracks each stage's
    health per user (ragra/health.py) and sends at most one notification per
    user if one of their components has been failing for
    FAILURE_ALERT_THRESHOLD consecutive ticks - re-armed automatically the
    next time that component succeeds.

    The process exit code is non-zero if *any* user had a failing stage,
    since the scheduled task has only one code to report.
    """
    from ragra.db import repo
    from ragra.logging_setup import configure_logging
    from ragra.web import sessions

    config = load_config()
    logger = configure_logging(config.ragra_home)
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info("tick start")

    exit_code = 0
    try:
        with connect_closing(config.db_path) as conn:
            # Housekeeping first, and deliberately not per-user: these prune
            # strictly by age (see repo.purge_old_tick_sessions), so running
            # them once per user would be wasted work, and running them only
            # for the users visited would leave a departed user's rows behind
            # forever.
            now = datetime.now(timezone.utc)
            repo.purge_old_tick_sessions(
                conn, older_than_iso=(now - timedelta(hours=48)).isoformat()
            )
            sessions.purge_expired_sessions(conn, now=now)

            users = repo.list_users(conn)
            logger.info("tick covering %d account(s)", len(users))

            for user in users:
                try:
                    rc, _errors = _tick_one_user(
                        conn, config, logger, user_id=user["id"], started_at=started_at
                    )
                except Exception as exc:  # noqa: BLE001 - one user must never abort the rest
                    logger.error("[user %s] tick failed unexpectedly: %s", user["id"], exc)
                    rc = 1
                if rc:
                    exit_code = 1
    except Exception as exc:  # noqa: BLE001 - last-resort guard; a tick must never leave a hung/corrupt process
        # Reached only for a failure outside any single user's run - opening
        # the database, or enumerating accounts. A per-user failure is
        # already contained above and recorded against that user.
        logger.error("tick failed unexpectedly: %s", exc)
        exit_code = 1

    logger.info("tick end (%.1fs, exit=%d)", time.monotonic() - started, exit_code)
    return exit_code


def cmd_brief(args: argparse.Namespace) -> int:
    from ragra.brief import build_deterministic_brief, build_full_brief

    config = load_config()
    now = datetime.now(timezone.utc)
    with connect_closing(config.db_path) as conn:
        user_id = acting_user_id(conn)
        if args.ai:
            print(build_full_brief(conn, user_id=user_id, now=now, hermes_bin=config.hermes_bin))
        else:
            print(build_deterministic_brief(conn, user_id=user_id, now=now))
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
                conn,
                user_id=acting_user_id(conn),
                hermes_bin=config.hermes_bin,
                now_iso=now.isoformat(),
                week_end_iso=week_end.isoformat(),
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


# ---------------------------------------------------------------------------
# Credential storage (P3-6)
# ---------------------------------------------------------------------------


def cmd_generate_credential_key(args: argparse.Namespace) -> int:
    """Mint an encryption key for stored Google credentials.

    Prints the key once, to stdout, and never writes it anywhere: Ragra
    must not be the thing that puts its own key next to the database it
    protects. A key invented by hand would look identical to this one and
    be enormously weaker, which is why this exists rather than a line of
    documentation saying "pick 32 random bytes".
    """
    from ragra import crypto

    print(crypto.generate_key())
    print()
    print(f"Set this as {crypto.KEY_ENV_VAR} in your environment (or .env).")
    print("Store it somewhere other than alongside the database - a key kept next")
    print("to the file it encrypts protects nothing. Losing it means every stored")
    print("Google authorization must be granted again; there is no recovery path.")
    return 0


def cmd_credentials_status(args: argparse.Namespace) -> int:
    """Show which Google services this account has authorized. Prints no
    secrets - it is written to be safe to paste into a conversation."""
    from ragra.adapters import google_credentials

    config = load_config()
    with connect_closing(config.db_path) as conn:
        for key, value in google_credentials.status(conn, user_id=acting_user_id(conn)).items():
            print(f"{key}: {value}")
    return 0


def cmd_credentials_import(args: argparse.Namespace) -> int:
    """Adopt existing on-disk Google tokens into the encrypted per-user
    store, so an already-granted authorization keeps working instead of
    needing a fresh consent flow.

    The source files are left in place deliberately (see
    google_credentials.import_from_file); delete them once this reports
    success and a sync has run.
    """
    from ragra import crypto
    from ragra.adapters import google_credentials

    config = load_config()
    if not crypto.is_configured():
        print(
            f"{crypto.KEY_ENV_VAR} is not set - nothing was imported. "
            "Generate one with: ragra generate-credential-key"
        )
        return 1

    sources = {
        google_credentials.CLASSROOM: config.classroom_paths.token_file,
        google_credentials.CALENDAR: config.calendar_paths.token_file,
    }

    imported = 0
    with connect_closing(config.db_path) as conn:
        user_id = acting_user_id(conn)
        for service, path in sources.items():
            if path is None:
                print(f"{service}: no token file configured")
                continue
            if google_credentials.import_from_file(
                conn, user_id=user_id, service=service, path=Path(path)
            ):
                print(f"{service}: imported into the encrypted store")
                imported += 1
            else:
                print(f"{service}: nothing to import")

    if imported:
        print()
        print("The original token files were left in place. Remove them once a sync")
        print("has confirmed the encrypted copies work.")
    return 0


def cmd_notify_status(args: argparse.Namespace) -> int:
    """Answer the one question this needs to answer: where are my reminders
    going? Destinations are shown in full - an address is not a secret, and
    masking it would make this useless."""
    from ragra.notifications.preferences import describe, load_preferences

    config = load_config()
    with connect_closing(config.db_path) as conn:
        user_id = acting_user_id(conn)
        for channel, state in describe(load_preferences(conn, user_id=user_id)).items():
            print(f"{channel}: {state}")

        if config.smtp_host is None:
            print()
            print("note: no SMTP relay is configured for this deployment, so email cannot be")
            print("      delivered even when switched on here. See .env.example.")
        if config.hermes_bin is None:
            print("note: HERMES_BIN is not set, so Hermes delivery is unavailable on this")
            print("      deployment even when switched on here.")
    return 0


def cmd_notify_set(args: argparse.Namespace) -> int:
    """Set this account's delivery destinations.

    Passing an empty value switches a channel off while keeping nothing
    behind; omitting a flag leaves that channel exactly as it was, so
    changing one destination never silently clears the other.
    """
    from ragra.notifications.preferences import (
        NotificationPreferences,
        describe,
        load_preferences,
        save_preferences,
    )

    config = load_config()
    with connect_closing(config.db_path) as conn:
        user_id = acting_user_id(conn)
        current = load_preferences(conn, user_id=user_id)

        email_to = current.email_to if args.email is None else (args.email.strip() or None)
        hermes_target = (
            current.hermes_target if args.hermes is None else (args.hermes.strip() or None)
        )

        updated = NotificationPreferences(
            email_enabled=bool(email_to),
            email_to=email_to,
            hermes_enabled=bool(hermes_target),
            hermes_target=hermes_target,
        )
        save_preferences(conn, user_id=user_id, preferences=updated)

        for channel, state in describe(updated).items():
            print(f"{channel}: {state}")
    return 0


def cmd_notify_adopt_env(args: argparse.Namespace) -> int:
    """Move this deployment's environment-configured destinations onto this
    account, once.

    The migration path for the existing single-user setup: without it, the
    owner's reminders would simply stop being delivered the moment
    destinations became per-user data.
    """
    from ragra.notifications.preferences import adopt_environment_defaults, describe, load_preferences

    config = load_config()
    with connect_closing(config.db_path) as conn:
        user_id = acting_user_id(conn)
        if adopt_environment_defaults(conn, config, user_id=user_id):
            print("Adopted the environment's destinations for this account:")
        else:
            print("Nothing adopted (this account already has preferences, or the")
            print("environment configures no destination). Current settings:")
        for channel, state in describe(load_preferences(conn, user_id=user_id)).items():
            print(f"  {channel}: {state}")
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

    sub.add_parser(
        "generate-credential-key",
        help="Print a new encryption key for stored Google credentials (never written to disk)",
    ).set_defaults(func=cmd_generate_credential_key)
    sub.add_parser(
        "credentials-status",
        help="Show which Google services this account has authorized (no secrets printed)",
    ).set_defaults(func=cmd_credentials_status)
    sub.add_parser(
        "credentials-import",
        help="Adopt existing on-disk Google tokens into the encrypted per-user store",
    ).set_defaults(func=cmd_credentials_import)

    sub.add_parser(
        "notify-status", help="Show where this account's reminders are delivered"
    ).set_defaults(func=cmd_notify_status)
    notify_set = sub.add_parser(
        "notify-set", help="Set this account's reminder delivery destinations"
    )
    notify_set.add_argument(
        "--email", help="Email address to deliver to; pass an empty value to switch email off"
    )
    notify_set.add_argument(
        "--hermes", help="Hermes delivery target; pass an empty value to switch Hermes off"
    )
    notify_set.set_defaults(func=cmd_notify_set)
    sub.add_parser(
        "notify-adopt-env",
        help="Adopt the environment's configured destinations for this account (one-time migration)",
    ).set_defaults(func=cmd_notify_adopt_env)

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
