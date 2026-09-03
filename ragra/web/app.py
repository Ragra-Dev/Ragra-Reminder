"""Ragra dashboard: FastAPI app.

Every route resolves the acting user through `current_user_id()` and passes
that id explicitly into the repository layer. Nothing here reads or writes
a row without naming its owner - which is what makes the ownership filter,
not the URL, decide what a request can see. Requesting another user's task
id returns 404 from the scoped query rather than the row.

Identity comes from the session cookie (see ragra/web/sessions.py), which
is issued only by the Google sign-in round trip in ragra/web/auth.py. One
deliberate exception exists and is described on `current_user_id`: a
single-user deployment on loopback with sign-in unconfigured continues to
work, because that is the shape Ragra has been running in and silently
locking its owner out of their own dashboard would be a worse outcome than
the risk it removes. That exception is narrow, testable, and can be switched
off.
"""

from __future__ import annotations

import ipaddress
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ragra import accounts as accounts_module
from ragra.adapters import google_credentials
from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.notifications.preferences import (
    NotificationPreferences,
    load_preferences,
    save_preferences,
)
from ragra.relevance.profile import load_raw_profile, save_profile
from ragra.timetable.enrollment import EnrolledCourse
from ragra.timetable.schedule import occurrences_for_local_day, weekly_class_from_row
from ragra.tz import format_stored_local, local_day_bounds, utc_iso
from ragra.web import auth, csrf, sessions

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class NotSignedIn(Exception):
    """No authenticated user for this request.

    Not an HTTPException: a browser navigating to a page should be sent to
    sign in, while a form post or an API-ish request should get a plain 401.
    The app-level handler decides which, so no route has to.
    """


# Headers a reverse proxy adds. Their presence means the socket peer is the
# proxy, not the person - so "the peer is loopback" stops meaning "this
# request came from this machine".
_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host")


def _is_loopback(request: Request | None) -> bool:
    """Whether the request genuinely came from this machine.

    The fallback below is only defensible because it cannot be reached from
    the network. Two things make that true:

    `request.client.host` is the actual socket peer, which a client cannot
    forge by sending a header - and it is parsed as an address rather than
    string-compared against "127.0.0.1", since loopback is a whole /8 plus
    ::1.

    But a reverse proxy on the same host would make every remote request
    look loopback, which would hand the owner's dashboard to anyone who
    could reach the proxy. So a request carrying proxy headers is not
    treated as local. Ragra does not trust those headers for anything -
    their mere presence is the signal, which is why a forged one only ever
    costs the forger the fallback.
    """
    if request is None or request.client is None:
        return False
    if any(header in request.headers for header in _PROXY_HEADERS):
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def current_user_id(
    conn: sqlite3.Connection,
    request: Request | None = None,
    *,
    settings: auth.AuthSettings | None = None,
    now: datetime | None = None,
) -> int:
    """The user this request acts as.

    Resolved from the session cookie. Fails closed: an absent, unknown, or
    expired session raises rather than falling back to "the first user",
    because silently picking an owner is exactly how one account ends up
    reading another's data.

    The one exception is the legacy single-user mode: when sign-in is not
    configured at all, the request came from loopback, and the database
    holds exactly one never-signed-in user, that user is the acting user.
    All three conditions are required. Configuring sign-in ends it, a second
    account ends it, and a request from anywhere but this machine never
    qualifies - so the deployment that would be exposed by it (bound to a
    public interface with no sign-in set up) is precisely the one it
    refuses.
    """
    session = sessions.lookup_session(
        conn,
        token=request.cookies.get(sessions.COOKIE_NAME) if request is not None else None,
        now=now or datetime.now(timezone.utc),
    )
    if session is not None:
        return session.user_id

    settings = settings if settings is not None else auth.load_auth_settings()
    if not settings.configured and _is_loopback(request):
        user_id = repo.unlinked_user_id(conn)
        if user_id is not None and len(repo.list_users(conn)) == 1:
            return user_id

    raise NotSignedIn()


def _todays_classes(conn, *, user_id: int, now: datetime) -> list:
    """Today's classes, computed on demand from the weekly pattern. Failing
    softly on purpose: a timetable problem must not blank the whole
    dashboard, whose deadline data is unaffected and still correct."""
    try:
        rows = repo.list_timetable_events(conn, user_id=user_id)
        return occurrences_for_local_day(
            [weekly_class_from_row(row) for row in rows], instant=now
        )
    except Exception:  # noqa: BLE001 - the schedule section degrades, the page does not
        return []

# How many MISSED tasks the main dashboard shows before pointing to the
# full list. Not a "historical" classification (see docs/ARCHITECTURE.md
# discussion) - purely a display limit, most-recent-deadline-first, so a
# long tail of old work doesn't dominate the daily view. Everything stays
# reachable via /missed regardless.
MISSED_SECTION_PREVIEW_LIMIT = 5

# Same rationale as the missed preview: the dashboard shows the most recent
# untriaged announcements, with the full list one click away.
ANNOUNCEMENT_PREVIEW_LIMIT = 5


def create_app(
    db_path: Path,
    *,
    auth_settings: auth.AuthSettings | None = None,
    identity_provider: auth.IdentityProvider | None = None,
) -> FastAPI:
    """Build the app.

    `auth_settings` and `identity_provider` are injectable so the sign-in
    security properties - state validation, PKCE, the allow-list, one-time
    adoption - can be tested end to end without a network round trip. A
    security control that can only be exercised against Google's live
    servers is a security control that does not get exercised.
    """
    app = FastAPI(title="Ragra")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    settings = auth_settings if auth_settings is not None else auth.load_auth_settings()

    # Every template gets the current request's CSRF token, so a form can
    # never be written without one being available - and the structural test
    # in tests/test_csrf.py fails if a form is written without using it.
    templates.env.globals["csrf_token_for"] = lambda request: csrf.token_for(
        csrf.session_token_from(request)
    )

    @app.middleware("http")
    async def _enforce_csrf(request: Request, call_next):
        """Applied to every unsafe request rather than route by route.

        Route-level checks are the version of this that gets forgotten: the
        failure mode is a new POST handler that nobody remembers to
        decorate, and it fails silently - the route works, it is just
        forgeable. Enforcing here means a route added tomorrow is covered
        without anyone doing anything.

        /auth/callback is not exempted: it is a GET, and its own single-use
        state parameter is what protects it (see ragra/web/auth.py).
        """
        if request.method not in csrf.UNSAFE_METHODS:
            return await call_next(request)

        submitted = request.headers.get(csrf.HEADER_NAME)
        if submitted is None:
            # Reading the body here consumes the stream, so it is replayed
            # for the route that follows. Without this the handler would
            # receive an empty form and the failure would look like a
            # validation bug rather than a plumbing one.
            body = await request.body()

            async def _replay():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = _replay  # noqa: SLF001 - the documented Starlette idiom
            form = await request.form()
            submitted = form.get(csrf.FIELD_NAME)
            request._receive = _replay  # noqa: SLF001 - form() consumed it again

        if not csrf.verify(submitted=submitted, session_token=csrf.session_token_from(request)):
            # 403, not 400: the request was well formed, it just was not
            # authorised to be made. Deliberately says nothing about
            # whether a session exists.
            return PlainTextResponse("Invalid or missing CSRF token", status_code=403)

        return await call_next(request)

    def _provider() -> auth.IdentityProvider:
        if identity_provider is not None:
            return identity_provider
        return auth.GoogleIdentityProvider(settings)

    def _acting_user(conn, request: Request) -> int:
        return current_user_id(conn, request, settings=settings)

    @app.exception_handler(NotSignedIn)
    def _not_signed_in(request: Request, _exc: NotSignedIn):
        """A browser navigating to a page is sent to sign in; anything else
        gets a bare 401. Both are deliberate: bouncing a form POST through a
        login redirect would silently discard what the user submitted, and
        answering a page request with a JSON error would strand them."""
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            target = auth.safe_redirect_target(request.url.path)
            suffix = f"?next={target}" if target != "/" else ""
            return RedirectResponse(f"/login{suffix}", status_code=303)
        return PlainTextResponse("Sign-in required", status_code=401)

    # --- Sign-in ----------------------------------------------------------
    #
    # Three routes and nothing else. The security of this flow rests on
    # properties enforced in ragra/web/auth.py - a single-use, expiring
    # state bound to the attempt that created it; PKCE so an intercepted
    # code cannot be redeemed; a verified ID token rather than a decoded
    # one; identity keyed on the Google subject rather than the email - so
    # these handlers stay thin enough to read in one sitting.

    @app.get("/login")
    def login(request: Request, next: str = ""):
        """Start sign-in. Idempotent and safe to reload: each visit begins a
        fresh attempt, and abandoned ones expire on their own."""
        if not settings.configured and identity_provider is None:
            return PlainTextResponse(
                "Sign-in is not configured on this deployment.", status_code=503
            )
        with connect_closing(db_path) as conn:
            url = auth.begin_sign_in(
                conn,
                _provider(),
                now=datetime.now(timezone.utc),
                redirect_to=next,
            )
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/callback")
    def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        now = datetime.now(timezone.utc)
        with connect_closing(db_path) as conn:
            # Spend the state first, whatever else is wrong with the
            # request: a callback that arrives with an error, or with no
            # code, must still not leave a reusable attempt behind.
            try:
                consumed = auth.consume_state(conn, state=state, now=now)
            except auth.AuthError:
                return PlainTextResponse("Sign-in could not be completed.", status_code=400)

            if error or not code:
                return PlainTextResponse("Sign-in could not be completed.", status_code=400)

            try:
                identity = _provider().exchange_code(
                    code=code, code_verifier=consumed.code_verifier
                )
                user_id = auth.resolve_user(conn, identity, settings, now=now)
            except auth.SignInRefused:
                # Distinguished from a failed exchange on purpose: being
                # told "not permitted" is actionable for a legitimate user
                # and reveals nothing an attacker did not already supply.
                return PlainTextResponse(
                    "This account is not permitted to sign in here.", status_code=403
                )
            except auth.AuthError:
                return PlainTextResponse("Sign-in could not be completed.", status_code=400)

            # A brand-new token, never one carried in from the request. This
            # is what makes session fixation impossible: an attacker cannot
            # plant a session and have it become authenticated.
            token = sessions.create_session(conn, user_id=user_id, now=now)

        response = RedirectResponse(consumed.redirect_to, status_code=303)
        _set_session_cookie(response, token, settings)
        return response

    @app.post("/logout")
    def logout(request: Request):  # CSRF-checked by the middleware above
        """POST only. A GET sign-out is a link an attacker can put in an
        image tag, which turns "log the user out" into something any page
        can do to them."""
        with connect_closing(db_path) as conn:
            sessions.revoke_session(conn, token=request.cookies.get(sessions.COOKIE_NAME))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(sessions.COOKIE_NAME, path="/")
        return response

    @app.get("/")
    def today(request: Request):
        now = datetime.now(timezone.utc)
        now_iso = utc_iso(now)
        # The campus calendar day, not the UTC one - see ragra/tz.py. These
        # disagree for five hours daily, during which a UTC boundary moves
        # work into or out of "due today".
        _day_start, day_end = local_day_bounds(now)
        end_of_today_iso = utc_iso(day_end)
        week_end_iso = utc_iso(now + timedelta(days=7))

        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            classes_today = _todays_classes(conn, user_id=user_id, now=now)
            announcements = repo.open_announcements(
                conn, user_id=user_id, limit=ANNOUNCEMENT_PREVIEW_LIMIT
            )
            personal_tasks = repo.manual_tasks(conn, user_id=user_id)
            overdue = repo.overdue_tasks(conn, user_id=user_id, now=now_iso)
            missed = repo.missed_tasks(
                conn, user_id=user_id, limit=MISSED_SECTION_PREVIEW_LIMIT
            )
            missed_total = repo.count_missed_tasks(conn, user_id=user_id)
            due_today = repo.tasks_due_between(
                conn, user_id=user_id, start_iso=now_iso, end_iso=end_of_today_iso
            )
            due_today_ids = {t["id"] for t in due_today}
            due_soon = [
                t for t in repo.tasks_due_between(
                    conn, user_id=user_id, start_iso=now_iso, end_iso=week_end_iso
                )
                if t["id"] not in due_today_ids
            ]
            needs_planning = conn.execute(
                """SELECT tasks.*, courses.course_code, courses.name AS course_name
                   FROM tasks JOIN courses ON courses.id = tasks.course_id
                   WHERE tasks.user_id = ? AND kind = 'ACTIONABLE' AND actual_deadline IS NULL
                   AND personal_deadline IS NULL AND status NOT IN ('COMPLETED', 'CANCELLED')
                   ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()
            needs_personal_target = repo.tasks_missing_personal_target(conn, user_id=user_id)
            scheduled_reminders = repo.upcoming_scheduled_reminders(
                conn, user_id=user_id, now=now_iso
            )
            recently_completed = repo.recently_completed_tasks(conn, user_id=user_id)

        priority = due_today[0] if due_today else (overdue[0] if overdue else (due_soon[0] if due_soon else None))

        return templates.TemplateResponse(
            request,
            "today.html",
            {
                "overdue": overdue,
                "missed": missed,
                "missed_total": missed_total,
                "missed_preview_limit": MISSED_SECTION_PREVIEW_LIMIT,
                "due_today": due_today,
                "due_soon": due_soon,
                "needs_planning": needs_planning,
                "needs_personal_target": needs_personal_target,
                "scheduled_reminders": scheduled_reminders,
                "recently_completed": recently_completed,
                "priority": priority,
                "classes_today": classes_today,
                "announcements": announcements,
                "personal_tasks": personal_tasks,
            },
        )

    @app.get("/missed")
    def missed_full(request: Request):
        with connect_closing(db_path) as conn:
            # No limit - this user's full list, still only this user's.
            missed = repo.missed_tasks(conn, user_id=_acting_user(conn, request))

        return templates.TemplateResponse(request, "missed.html", {"missed": missed})

    @app.get("/brief")
    def brief(request: Request):
        from fastapi.responses import PlainTextResponse

        from ragra.brief import build_deterministic_brief

        with connect_closing(db_path) as conn:
            text = build_deterministic_brief(
                conn, user_id=_acting_user(conn, request), now=datetime.now(timezone.utc)
            )
        return PlainTextResponse(text)

    @app.get("/tasks/{task_id}")
    def task_detail(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            # Scoped lookup: another user's task id is indistinguishable from
            # a nonexistent one, so this is a 404 and not a disclosure.
            task = repo.get_task_by_id(conn, user_id=user_id, task_id=task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            reminders = repo.reminders_for_task(conn, user_id=user_id, task_id=task_id)
            history = repo.history_for_task(conn, user_id=user_id, task_id=task_id)

        return templates.TemplateResponse(
            request,
            "task_detail.html",
            {"task": task, "reminders": reminders, "history": history},
        )

    @app.post("/tasks/{task_id}/complete")
    def complete_task(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            repo.mark_completed(conn, user_id=user_id, task_id=task_id)
            repo.cancel_pending_reminders(conn, user_id=user_id, task_id=task_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{task_id}/personal-deadline")
    def set_personal_deadline(
        request: Request, task_id: int, personal_deadline: str = Form(...)
    ):
        # Allowed on every task, Classroom-sourced included: a personal
        # completion target is Ragra-owned data about the user's own plan and
        # never touches anything Classroom is authoritative for. See
        # docs/INTERFACES.md contract #5.
        with connect_closing(db_path) as conn:
            repo.set_personal_deadline(
                conn, user_id=_acting_user(conn, request), task_id=task_id,
                personal_deadline=personal_deadline,
            )
        return RedirectResponse("/", status_code=303)

    # --- Manual tasks -----------------------------------------------------
    #
    # Every route below declares each accepted form field by name. That is
    # the first and most important layer of the write guard: a route that
    # does not declare `title` structurally cannot receive one, so no amount
    # of extra POST data can reach a Classroom-authoritative column. The
    # repo-layer TaskSourceViolation check is the second layer, and input
    # validation is the third. There is no auth layer behind any of this.

    @app.get("/tasks")
    def tasks_page(request: Request):
        with connect_closing(db_path) as conn:
            personal = repo.manual_tasks(conn, user_id=_acting_user(conn, request))
        return templates.TemplateResponse(request, "tasks.html", {"tasks": personal})

    @app.post("/tasks/new")
    def create_task(
        request: Request,
        title: str = Form(...),
        description: str = Form(""),
        actual_deadline: str = Form(""),
        personal_deadline: str = Form(""),
    ):
        with connect_closing(db_path) as conn:
            try:
                repo.create_manual_task(
                    conn,
                    user_id=_acting_user(conn, request),
                    title=title,
                    description=description or None,
                    actual_deadline=_clean_deadline(actual_deadline),
                    personal_deadline=_clean_deadline(personal_deadline),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/edit")
    def edit_task(
        request: Request,
        task_id: int,
        title: str = Form(...),
        description: str = Form(""),
        actual_deadline: str = Form(""),
    ):
        with connect_closing(db_path) as conn:
            try:
                repo.update_manual_task(
                    conn,
                    user_id=_acting_user(conn, request),
                    task_id=task_id,
                    title=title,
                    description=description or None,
                    actual_deadline=_clean_deadline(actual_deadline),
                )
            except repo.TaskSourceViolation as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            try:
                repo.cancel_task(conn, user_id=user_id, task_id=task_id)
            except repo.TaskSourceViolation as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            repo.cancel_pending_reminders(conn, user_id=user_id, task_id=task_id)
        return RedirectResponse("/tasks", status_code=303)

    # --- Announcements ----------------------------------------------------
    #
    # Fully deterministic: open it, optionally turn it into a task you own,
    # or archive it. No AI anywhere in this path - the announcement text is
    # never summarised, interpreted, or used to invent a deadline.

    @app.get("/announcements")
    def announcements_page(request: Request):
        with connect_closing(db_path) as conn:
            rows = repo.open_announcements(conn, user_id=_acting_user(conn, request))
        return templates.TemplateResponse(request, "announcements.html", {"announcements": rows})

    @app.post("/announcements/{task_id}/create-task")
    def create_task_from_announcement(
        request: Request,
        task_id: int,
        title: str = Form(""),
        actual_deadline: str = Form(""),
        personal_deadline: str = Form(""),
    ):
        with connect_closing(db_path) as conn:
            try:
                repo.create_task_from_announcement(
                    conn,
                    user_id=_acting_user(conn, request),
                    announcement_task_id=task_id,
                    title=title.strip() or None,
                    actual_deadline=_clean_deadline(actual_deadline),
                    personal_deadline=_clean_deadline(personal_deadline),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/announcements", status_code=303)

    @app.post("/announcements/{task_id}/archive")
    def archive_announcement(request: Request, task_id: int):
        with connect_closing(db_path) as conn:
            repo.archive_task(
                conn, user_id=_acting_user(conn, request), task_id=task_id
            )
        return RedirectResponse("/announcements", status_code=303)

    @app.get("/deliveries")
    def deliveries(request: Request):
        with connect_closing(db_path) as conn:
            rows = repo.recent_notification_deliveries(
                conn, user_id=_acting_user(conn, request), limit=100
            )
        return templates.TemplateResponse(request, "deliveries.html", {"deliveries": rows})

    # --- Account settings --------------------------------------------------
    #
    # The roadmap's Phase 3 frontend work lists a profile editor, notification
    # preferences, and a delete-account flow explicitly - not just the storage
    # and CLI commands underneath them. These three routes are that UI. What
    # is deliberately NOT here is a web-triggered Google consent screen for
    # Classroom/Calendar access: that has always been a local, interactive
    # CLI flow (`ragra classroom-auth` / `calendar-auth`) that only proceeds
    # after explicit human go-ahead at a terminal, and Phase 3 did not change
    # that boundary - it only changed where the resulting token is stored
    # (see ragra/adapters/google_credentials.py). This page shows that
    # connection status read-only and points at the CLI for changing it.

    @app.get("/account")
    def account_page(request: Request):
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            user_row = repo.get_user(conn, user_id=user_id)
            profile = load_raw_profile(conn, user_id=user_id)
            preferences = load_preferences(conn, user_id=user_id)
            credentials = google_credentials.status(conn, user_id=user_id)

        return templates.TemplateResponse(
            request,
            "account.html",
            {
                "display_name": user_row["display_name"] if user_row else None,
                "profile": profile,
                "enrollment_text": _enrollment_to_text(profile.enrollment),
                "preferences": preferences,
                "credentials": credentials,
            },
        )

    @app.post("/account/profile")
    def update_account_profile(
        request: Request,
        program: str = Form(...),
        batch_year: str = Form(""),
        enrollment_start_year: str = Form(...),
        enrollment_start_term: str = Form(...),
        enrollment: str = Form(""),
    ):
        try:
            start_year = int(enrollment_start_year.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Enrollment start year must be a whole number"
            ) from exc
        try:
            courses = _parse_enrollment_text(enrollment)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            try:
                save_profile(
                    conn,
                    user_id=user_id,
                    program=program.strip(),
                    batch_year=batch_year.strip() or None,
                    enrollment_start_year=start_year,
                    enrollment_start_term=enrollment_start_term.strip().upper(),
                    enrollment=courses,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/account", status_code=303)

    @app.post("/account/notifications")
    def update_account_notifications(
        request: Request,
        email_enabled: str = Form(""),
        email_to: str = Form(""),
        hermes_enabled: str = Form(""),
        hermes_target: str = Form(""),
    ):
        # A checkbox's value is present only when checked, so "enabled" is
        # derived from presence - but the destination itself is saved
        # regardless of the toggle, so switching a channel off never throws
        # away the address it will be switched back on with.
        clean_email = email_to.strip() or None
        clean_hermes = hermes_target.strip() or None
        preferences = NotificationPreferences(
            email_enabled=bool(email_enabled) and clean_email is not None,
            email_to=clean_email,
            hermes_enabled=bool(hermes_enabled) and clean_hermes is not None,
            hermes_target=clean_hermes,
        )
        with connect_closing(db_path) as conn:
            save_preferences(conn, user_id=_acting_user(conn, request), preferences=preferences)
        return RedirectResponse("/account", status_code=303)

    @app.get("/account/delete")
    def account_delete_page(request: Request):
        with connect_closing(db_path) as conn:
            summary = accounts_module.preview_deletion(
                conn, user_id=_acting_user(conn, request)
            )
        return templates.TemplateResponse(
            request, "account_delete.html", {"lines": accounts_module.describe(summary)}
        )

    @app.post("/account/delete")
    def account_delete_route(request: Request, confirm: str = Form("")):
        # Typed confirmation, not a checkbox: this is the one action in
        # Ragra that cannot be undone by using the product again, and a
        # checkbox is too easy to tick on the way past. Case-insensitive
        # and whitespace-trimmed on purpose - the word is a deliberate
        # usability speed bump, not the security boundary. That boundary
        # is the session and CSRF token this route already requires.
        if confirm.strip().lower() != "delete":
            raise HTTPException(status_code=400, detail='Type "delete" to confirm.')

        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            accounts_module.delete_account(conn, user_id=user_id)
            # The cascade already removed every session row for this user
            # (sessions.user_id -> ON DELETE CASCADE), including the one this
            # request is using - the cookie itself is cleared below only so
            # the browser stops sending a token that no longer resolves to
            # anything.

        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(sessions.COOKIE_NAME, path="/")
        return response

    return app


def _set_session_cookie(response, token: str, settings: auth.AuthSettings) -> None:
    """Set the session cookie with the flags that make it a session cookie
    rather than a liability.

    httponly  - script cannot read it, so an XSS bug cannot exfiltrate the
                session even though it could still act as the user.
    samesite  - "lax" stops another site's form post from carrying it,
                which is the browser-side half of the CSRF defence in
                ragra/web/csrf.py.
    secure    - derived from the redirect URI's scheme (see
                auth.load_auth_settings), so an HTTPS deployment cannot
                accidentally send it in the clear.
    """
    response.set_cookie(
        sessions.COOKIE_NAME,
        token,
        max_age=int(sessions.ABSOLUTE_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


def _parse_enrollment_text(text: str) -> tuple[EnrolledCourse, ...]:
    """Parse the enrollment textarea, one course per line:

        Course Name | Section | REGULAR or REPEAT | batch year | aliases,comma,separated

    Only the first three fields are required. A plain-text, line-per-course
    format rather than a dynamic add-row widget is a deliberate scope
    decision for this phase (see docs/PROJECT_STATUS.md) - it needs no
    client-side JavaScript and reuses EnrolledCourse's own validation
    unchanged, at the cost of being less friendly to edit than a real form
    would be. A richer editor is future frontend work, not a blocked
    dependency of anything in Phase 3.

    Raises ValueError naming the 1-indexed line on any problem, so the
    route can turn it directly into a 400 the user can act on.
    """
    courses: list[EnrolledCourse] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            raise ValueError(
                f"line {lineno}: expected at least 'Course Name | Section | REGULAR or REPEAT'"
            )
        name, section, enrollment_type = parts[0], parts[1], parts[2].upper()
        if not name or not section:
            raise ValueError(f"line {lineno}: course name and section are both required")
        batch_year = parts[3] if len(parts) > 3 and parts[3] else None
        aliases = (
            tuple(a.strip() for a in parts[4].split(",") if a.strip())
            if len(parts) > 4 and parts[4]
            else ()
        )
        try:
            courses.append(
                EnrolledCourse(
                    name, section, enrollment_type, batch_year=batch_year, aliases=aliases
                )
            )
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from exc
    return tuple(courses)


def _enrollment_to_text(enrollment: tuple[EnrolledCourse, ...]) -> str:
    """The inverse of `_parse_enrollment_text`, for pre-filling the textarea
    with a user's already-saved enrollment."""
    lines = []
    for course in enrollment:
        parts = [course.course_name, course.section, course.enrollment_type]
        if course.batch_year or course.aliases:
            parts.append(course.batch_year or "")
        if course.aliases:
            parts.append(",".join(course.aliases))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _clean_deadline(value: str) -> str | None:
    """Empty means "no deadline", never "now". A supplied value must parse
    as a real date/datetime - storing an unparseable string would produce a
    row that every deadline comparison silently mis-sorts."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a valid date or date-time") from exc
    return text


def _default_db_path() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    home = Path(local_appdata) / "ragra" if local_appdata else Path.home() / ".ragra"
    return home / "ragra.db"


# Module-level ASGI app so `uvicorn ragra.web.app:app` (and
# `python -m ragra.web.app`, below) can find an entry point. Uses a fixed
# default database location; ragra/config.py will take over resolving this
# once the CLI entrypoint is wired up.
app = create_app(_default_db_path())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8731)
