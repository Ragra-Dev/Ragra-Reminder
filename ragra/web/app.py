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

import sys
import ipaddress
import os
import hmac
import secrets
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ragra import accounts as accounts_module
from ragra import crypto
from ragra.adapters import credential_health, google_credentials
from ragra.db import repo
from ragra.db.connection import connect_closing
from ragra.adapters.notify import normalize_whatsapp_number, send_to_all_providers
from ragra.notifications.preferences import (
    NotificationPreferences,
    channels_needing_welcome,
    load_preferences,
    save_preferences,
    welcome_notification,
    welcome_providers,
)
from ragra.relevance.profile import load_raw_profile, save_profile
from ragra.timetable.enrollment import EnrolledCourse
from ragra.timetable.schedule import occurrences_for_local_day, weekly_class_from_row
from ragra.tz import format_stored_local, local_day_bounds, utc_iso
from ragra import chat as chat_module
from ragra.web import auth, csrf, google_reconnect, sessions, telegram as telegram_web, whatsapp as whatsapp_web

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


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




def _classroom_sync_problem(conn, user_id: int) -> dict | None:
    """A plain-language note when the last Classroom sync failed, so a silent
    failure (for example assignments refused because that permission box was
    not ticked, while announcements still load) is visible to the person."""
    row = repo.get_sync_state(conn, user_id=user_id, source="classroom")
    if row is None or row["status"] != "ERROR" or not row["last_error"]:
        return None
    err = str(row["last_error"])
    low = err.lower()
    assignments = "coursework sync failed" in low
    denied = any(w in low for w in ("403", "insufficient", "permission", "scope", "forbidden", "not authorized"))
    if assignments and denied:
        text = "Ragra can't read your assignments from Classroom, so new deadlines won't appear. Announcements still work."
        fix = "Reconnect Classroom on the Profile page and tick every permission box on Google's screen."
    elif assignments:
        text = "Ragra couldn't read your assignments from Classroom on the last check, so new deadlines may be missing."
        fix = "It will retry automatically. If this stays, reconnect Classroom on the Profile page."
    else:
        text = "The last Classroom check had a problem, so some items may be missing."
        fix = "It will retry automatically. If this stays, reconnect Classroom on the Profile page."
    return {"text": text, "fix": fix, "detail": err[:300], "when": row["last_synced_at"]}

def _deadline_card(task, now: datetime) -> dict:
    """One task as a deadline card: campus-time date parts, a friendly
    relative day ("Today", "Tomorrow", a weekday) and a priority worked out
    from how soon it is due (tasks have no priority field of their own)."""
    from ragra.tz import parse_instant, to_local

    when_local = to_local(parse_instant(str(task["actual_deadline"])))
    days = (when_local.date() - to_local(now).date()).days
    if days < 0:
        level, rel, group = "High", "Overdue", "overdue"
    elif days == 0:
        level, rel, group = "High", "Today", "today"
    elif days == 1:
        level, rel, group = "Medium", "Tomorrow", "tomorrow"
    else:
        level = "Medium" if days <= 3 else "Low"
        rel = when_local.strftime("%A") if days < 7 else when_local.strftime("%a %d %b").replace(" 0", " ")
        group = "week" if days < 7 else "later"
    hour = when_local.strftime("%I").lstrip("0") or "12"
    hours_left = (parse_instant(str(task["actual_deadline"])) - now).total_seconds() / 3600
    if hours_left < 0:
        left = ""
    elif hours_left < 1:
        left = f"{max(1, int(hours_left * 60))} min left"
    elif hours_left < 24:
        h, m = int(hours_left), int((hours_left % 1) * 60)
        left = f"{h}h {m}m left" if m else f"{h}h left"
    else:
        left = f"{int(hours_left // 24)}d left"
    return {
        "hours_left": round(hours_left, 3),
        "left": left,
        "id": task["id"],
        "title": task["title"],
        "course": task["course_code"] or task["course_name"],
        "month": when_local.strftime("%b"),
        "day": when_local.day,
        "weekday": when_local.strftime("%a"),
        "rel": rel,
        "time": f"{hour}:{when_local.strftime('%M %p')}",
        "level": level,
        "group": group,
        "days": days,
    }

def create_app(
    db_path: Path,
    *,
    auth_settings: auth.AuthSettings | None = None,
    identity_provider: auth.IdentityProvider | None = None,
    data_auth_provider: google_reconnect.GoogleDataAuthProvider | None = None,
) -> FastAPI:
    """Build the app.

    `auth_settings` and `identity_provider` are injectable so the sign-in
    security properties - state validation, PKCE, the allow-list, one-time
    adoption - can be tested end to end without a network round trip. A
    security control that can only be exercised against Google's live
    servers is a security control that does not get exercised.

    `data_auth_provider` is the same principle applied to the "Reconnect
    Google" flow (ragra/web/google_reconnect.py): injectable so its state/
    PKCE/ownership handling is testable without a real Classroom/Calendar
    consent round trip.
    """
    app = FastAPI(title="Ragra")
    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")  # logo and app icons; public
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    settings = auth_settings if auth_settings is not None else auth.load_auth_settings()

    # Every template gets the current request's CSRF token, so a form can
    # never be written without one being available - and the structural test
    # in tests/test_csrf.py fails if a form is written without using it.
    templates.env.globals["csrf_token_for"] = lambda request: csrf.token_for(
        csrf.session_token_from(request)
    )

    def _nav_user(request: Request) -> dict | None:
        """Who to show in the navigation bar. Best effort and display-only:
        it never gates anything (every route resolves its own acting user),
        so any failure here simply means the bar shows no account chip."""
        try:
            with connect_closing(db_path) as conn:
                row = repo.get_user(conn, user_id=current_user_id(conn, request, settings=settings))
        except Exception:  # noqa: BLE001 - cosmetic; the page must render regardless
            return None
        if row is None:
            return None
        name = (row["display_name"] or row["email"] or "Account").strip()
        return {
            "name": name,
            "email": row["email"],
            "picture": row["picture_url"],
            "initial": name[:1].upper() or "?",
        }

    templates.env.globals["nav_user"] = _nav_user

    def _friendly_when(value) -> str:
        """An ISO instant as a short campus-time label ("Fri 23 Oct, 9:25 pm").
        Display only: anything that does not parse is shown unchanged rather
        than hidden, so a bad value is visible instead of silently blank."""
        if not value:
            return ""
        if "T" not in str(value):
            try:  # a plain date has no time to convert: just make it readable
                d = datetime.strptime(str(value), "%Y-%m-%d")
                return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
            except ValueError:
                return str(value)
        try:
            from ragra.tz import parse_instant, to_local

            local = to_local(parse_instant(str(value)))
        except Exception:  # noqa: BLE001 - never break a page over a label
            return str(value)
        hour = local.strftime("%I").lstrip("0") or "12"
        return f"{local.strftime('%a')} {local.day} {local.strftime('%b')}, {hour}:{local.strftime('%M %p').lower()}"

    templates.env.filters["when"] = _friendly_when

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
            fallback = None
            if (
                request.method == "GET"
                and sessions.COOKIE_NAME not in request.cookies
                and csrf.FALLBACK_COOKIE not in request.cookies
            ):
                # No session to derive a token from: hand this visitor a private
                # CSRF cookie, and make it visible to this very request so the
                # page being rendered carries a matching token.
                fallback = secrets.token_urlsafe(32)
                headers = [(k, v) for k, v in request.scope["headers"] if k != b"cookie"]
                existing = b"; ".join(v for k, v in request.scope["headers"] if k == b"cookie")
                pair = f"{csrf.FALLBACK_COOKIE}={fallback}".encode()
                headers.append((b"cookie", existing + b"; " + pair if existing else pair))
                request.scope["headers"] = headers
            response = await call_next(request)
            if fallback:
                response.set_cookie(
                    csrf.FALLBACK_COOKIE, fallback, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", path="/",
                )
            return response

        if request.url.path in ("/whatsapp/webhook", "/telegram/webhook"):
            return await call_next(request)  # authenticated by their own secret; checked in the route

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

    def _data_provider() -> google_reconnect.GoogleDataAuthProvider:
        if data_auth_provider is not None:
            return data_auth_provider
        from ragra.adapters.calendar import CALENDAR_SCOPES
        from ragra.adapters.classroom import CLASSROOM_SCOPES
        from ragra.config import load_config

        config = load_config()
        base = (config.web_base_url or f"http://{config.web_host}:{config.web_port}").rstrip("/")
        return google_reconnect.GoogleOAuthDataProvider(
            client_secrets_file=config.classroom_paths.credentials_file,
            redirect_uri=f"{base}/account/google/callback",
            classroom_scopes=CLASSROOM_SCOPES,
            calendar_scopes=CALENDAR_SCOPES,
        )

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

    @app.get("/manifest.webmanifest")
    def web_manifest():
        """Makes Ragra installable: the home-screen name, icon and colours."""
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "name": "Ragra Reminder", "short_name": "Ragra",
                "description": "Never miss what's next. Classroom deadlines, announcements and class alerts.",
                "start_url": "/chat", "scope": "/", "display": "standalone",
                "background_color": "#F1F5FF", "theme_color": "#2563EB",
                "icons": [
                    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                    {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    def service_worker():
        """Served from the root so its scope covers the whole app."""
        from fastapi.responses import FileResponse

        return FileResponse(_STATIC_DIR / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/privacy")
    def privacy(request: Request):
        """Public: Google requires a reachable privacy policy, and a person
        deciding whether to sign in has to be able to read it first."""
        import os

        return templates.TemplateResponse(
            request, "privacy.html", {"contact_email": (os.environ.get("RAGRA_CONTACT_EMAIL") or "").strip()}
        )

    @app.get("/welcome")
    def welcome(request: Request):
        """Public front page. `/` itself still sends a signed-out visitor
        straight to sign-in; this is the page that explains what Ragra is,
        and the one to give Google as the application home page."""
        return templates.TemplateResponse(request, "welcome.html", {})

    @app.get("/help")
    def help_page(request: Request):
        return templates.TemplateResponse(request, "help.html", {})

    @app.get("/terms")
    def terms(request: Request):
        return templates.TemplateResponse(request, "terms.html", {})

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

    # --- Reconnect Google (Classroom/Calendar data authorization) ---------
    #
    # Distinct from sign-in above: these two routes let an already-signed-in
    # user grant/replace Ragra's Classroom or Calendar authorization from
    # the account page instead of a terminal command. Same Google consent
    # screen, same OAuth client, same PKCE/single-use-state protections as
    # ragra/web/auth.py - only the redirect target and the scopes differ.
    # ragra classroom-auth/calendar-auth remain available unchanged; this is
    # an additional, lower-friction path to the same result, not a
    # replacement for the CLI flow.

    # A reconnect *attempt* failing says nothing about the credential
    # currently in storage, so none of the failure paths below write to
    # credential_health: that table describes whether the stored
    # credential works, and a user who opens the consent screen and
    # changes their mind has not changed it. Writing a failure there would
    # flip a perfectly healthy integration to "degraded" until the next
    # tick corrected it. Only a *completed* reconnect writes, because only
    # then is there a new credential whose health is known.
    _RECONNECT_FAILED = "Reconnect could not be completed. Nothing was changed - your existing Google connection is untouched."

    @app.post("/account/google/reconnect/{service}")
    def google_reconnect_begin(request: Request, service: str):  # CSRF-checked by the middleware above
        if service not in google_reconnect.SERVICES:
            raise HTTPException(status_code=404)
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            try:
                url = google_reconnect.begin_reconnect(
                    conn, _data_provider(), user_id=user_id, service=service, now=datetime.now(timezone.utc)
                )
            except google_reconnect.ReconnectNotConfigured as exc:
                # The reason goes to the server log only, never to the browser.
                print(f"google reconnect (begin) not configured: {exc}", file=sys.stderr)
                return PlainTextResponse(
                    "Google reconnect is not configured on this deployment (begin).", status_code=503
                )
            except Exception:  # noqa: BLE001 - never surface an internal error (or a local path) to a browser
                return PlainTextResponse(_RECONNECT_FAILED, status_code=400)
        return RedirectResponse(url, status_code=303)

    @app.get("/account/google/callback")
    def google_reconnect_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        now = datetime.now(timezone.utc)
        with connect_closing(db_path) as conn:
            # Spend the state first, whatever else is wrong with the
            # request - same reasoning as /auth/callback.
            try:
                consumed = google_reconnect.consume_reconnect(conn, state=state, now=now)
            except google_reconnect.ReconnectError:
                return PlainTextResponse("Reconnect could not be completed.", status_code=400)

            # The user declined at Google's consent screen, or came back
            # without a code. Their existing credential is untouched.
            if error or not code:
                print(f"google reconnect: no code (google error={error!r})", file=sys.stderr)
                return PlainTextResponse(_RECONNECT_FAILED, status_code=400)

            try:
                credentials = _data_provider().exchange_code(
                    service=consumed.service, code=code, code_verifier=consumed.code_verifier
                )
            except google_reconnect.ReconnectNotConfigured as exc:
                print(f"google reconnect (callback) not configured: {exc}", file=sys.stderr)
                return PlainTextResponse(
                    "Google reconnect is not configured on this deployment (callback).", status_code=503
                )
            except Exception as exc:  # noqa: BLE001 - the real provider can also raise network, JSON and
                # filesystem errors; none of their messages (which can carry request detail or a
                # local path) may reach a browser, and none of them justify a 500.
                print(f"google reconnect: code exchange failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                return PlainTextResponse(_RECONNECT_FAILED, status_code=400)

            if not getattr(credentials, "refresh_token", None):
                # Enforced here, not only inside the real provider: a
                # credential the unattended tick can't actually use later
                # must never be accepted as "reconnected", regardless of
                # which provider produced it.
                print("google reconnect: Google returned no refresh token", file=sys.stderr)
                return PlainTextResponse(_RECONNECT_FAILED, status_code=400)

            # Verified before storing, not after: a grant that is missing
            # scopes this service needs would otherwise be recorded as
            # HEALTHY and fail on some later unattended tick, and a grant
            # for the *other* service would be filed under this one.
            absent = google_reconnect.missing_scopes(
                service=consumed.service, granted=getattr(credentials, "scopes", None)
            )
            if absent:
                granted_now = sorted(getattr(credentials, "scopes", None) or ())
                print(f"google reconnect: missing {list(absent)}; google granted {granted_now}", file=sys.stderr)
                return PlainTextResponse(
                    "Reconnect could not be completed: the approval did not include everything "
                    f"Ragra needs for {consumed.service}. Missing: {', '.join(absent)}. "
                    "Nothing was changed - your existing Google connection is untouched.",
                    status_code=400,
                )

            try:
                google_credentials.store(
                    conn, user_id=consumed.user_id, service=consumed.service, payload=credentials.to_json()
                )
            except crypto.CredentialKeyMissing:
                return PlainTextResponse(
                    "This deployment has no credential encryption key configured - an operator must set "
                    "RAGRA_CREDENTIAL_KEY before reconnecting can succeed.",
                    status_code=503,
                )
            except Exception as exc:  # noqa: BLE001 - storage failure must not 500 or leak a path either
                print(f"google reconnect: storing credential failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                return PlainTextResponse(_RECONNECT_FAILED, status_code=400)

            credential_health.record_success(conn, user_id=consumed.user_id, service=consumed.service)

        return RedirectResponse("/account", status_code=303)

    @app.post("/account/classroom/check")
    def classroom_check(request: Request):
        """Test each Classroom permission with a real call and say, in plain
        words, which part works and which is refused. CSRF-checked by the
        middleware. Reads only this account's Classroom; nothing is stored."""
        from fastapi.responses import JSONResponse

        from ragra.adapters import classroom as classroom_adapter
        from ragra.adapters import user_clients
        from ragra.config import load_config

        def step(name, label, status, detail):
            row = {"name": name, "label": label, "status": status, "detail": detail}
            scope = classroom_adapter.SCOPE_FOR_READ.get(name)
            if status == "fail" and scope:
                row["allow"] = {"service": "classroom"}   # lets the page offer an Allow button
            return row

        def refused(exc) -> bool:
            low = str(exc).lower()
            return any(w in low for w in ("403", "insufficient", "permission", "scope", "forbidden"))

        steps: list[dict] = []
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            base_rows = google_reconnect.permission_rows(conn, user_id=user_id)
            try:
                client = user_clients.classroom_client_for(conn, load_config(), user_id=user_id)
            except user_clients.UserCredentialsUnavailable:
                return JSONResponse({"ok": False, "steps": [step(
                    "connect", "Classroom connection", "fail",
                    "Classroom isn't connected. Press Connect Classroom above.")]})
            except Exception as exc:  # noqa: BLE001 - report, never crash the page
                return JSONResponse({"ok": False, "steps": [step(
                    "connect", "Classroom connection", "fail", f"Couldn't start: {str(exc)[:160]}")]})

        # 1. classes
        try:
            courses = client.list_courses()
        except Exception as exc:  # noqa: BLE001
            msg = ("Google refused: the 'view your classes' permission is missing." if refused(exc)
                   else f"Couldn't read classes: {str(exc)[:160]}")
            return JSONResponse({"ok": False, "steps": [step("courses", "Your classes", "fail", msg)]})
        active = [c for c in courses if (c.get("courseState") or "ACTIVE") == "ACTIVE"]
        steps.append(step("courses", "Your classes", "ok",
                          f"{len(active)} active class{'' if len(active) == 1 else 'es'} found"
                          + (f" ({len(courses) - len(active)} archived or inactive)" if len(courses) != len(active) else "") + "."))

        # Is this account a teacher in any active class? A teacher's assignments are not
        # readable with the student permission Ragra asks for, whatever boxes are ticked.
        try:
            taught = client.list_taught_course_ids() & {c["id"] for c in active}
        except Exception:  # noqa: BLE001 - only used to explain a failure
            taught = set()
        if taught:
            steps.append(step("role", "Your role", "info",
                              f"You are the TEACHER of {len(taught)} of these classes. Ragra reads a class as a student."))

        # 2-4. assignments, announcements, materials across the active classes
        def scan(label, name, fetch, describe, missing_hint):
            total = dated = 0
            failure = None
            for course in active[:15]:
                try:
                    items = fetch(course["id"])
                except Exception as exc:  # noqa: BLE001
                    failure = exc
                    break
                total += len(items)
                dated += sum(1 for i in items if i.get("dueDate"))
            if failure is not None:
                if refused(failure) and taught and name == "coursework":
                    msg = (f"As a teacher, {label} is not allowed. Google refused, because you are the teacher of this class. "
                           "Ragra's permission covers a student's own assignments. Check with a student account that has joined the class.")
                else:
                    msg = missing_hint if refused(failure) else f"Couldn't read: {str(failure)[:160]}"
                # Google's own reason, so the real cause is visible (no keys or tokens are ever in it).
                msg += f" [Google said: {str(failure)[:200]}]"
                steps.append(step(name, label, "fail", msg))
            else:
                steps.append(step(name, label, "ok", describe(total, dated)))

        scan("Assignments and deadlines", "coursework", client.list_course_work,
             lambda t, d: (f"{t} assignment{'' if t == 1 else 's'} found, {d} with a due date."
                           + ("" if t == 0 else (" Only assignments with a due date appear on Deadlines." if d < t else ""))),
             "Google refused: the 'view your course work' permission is missing. Reconnect Classroom and tick it.")
        scan("Announcements", "announcements", client.list_announcements,
             lambda t, d: f"{t} announcement{'' if t == 1 else 's'} found.",
             "Google refused: the 'view announcements' permission is missing.")
        scan("Class materials", "materials", client.list_course_materials,
             lambda t, d: f"{t} material{'' if t == 1 else 's'} found.",
             "Google refused: the 'view class materials' permission is missing.")
        ok = all(x["status"] in ("ok", "info") for x in steps)

        # Everything reads fine: bring this account up to date right now, the same
        # sync the background loop runs. This also clears an old "can't read your
        # assignments" notice instead of leaving it until the next scheduled check.
        if ok:
            from ragra.sync.classroom_sync import sync_classroom

            try:
                with connect_closing(db_path) as conn:
                    summary = sync_classroom(conn, client, user_id=_acting_user(conn, request))
                if summary.errors:
                    steps.append(step("sync", "Update your deadlines", "fail",
                                      f"Couldn't finish updating: {summary.errors[0][:160]}"))
                    ok = False
                else:
                    steps.append(step("sync", "Update your deadlines", "ok",
                                      f"Done. {summary.tasks_created} new and {summary.tasks_updated} updated. "
                                      "They now show on the Deadlines page."))
            except Exception as exc:  # noqa: BLE001 - report, never crash the page
                steps.append(step("sync", "Update your deadlines", "fail", f"Couldn't update: {str(exc)[:160]}"))
                ok = False

        # One merged list: each permission Ragra needs, allowed or not, with what the check found.
        by_scope = {}
        for st in steps:
            scope = classroom_adapter.SCOPE_FOR_READ.get(st["name"])
            if scope:
                by_scope[scope] = st
        merged = []
        for row in base_rows:
            probe = by_scope.get(row["scope"])
            granted, detail = row["granted"], ""
            if probe is not None and probe["name"] != "sync":
                granted, detail = probe["status"] == "ok", probe["detail"]
            merged.append({**row, "granted": granted, "detail": detail})
        notes = []
        if taught:
            n = len(taught)
            notes.append(f"You are the TEACHER of {n} of your classes. Ragra reads a class as a student.")
        return JSONResponse({"ok": ok, "steps": steps, "permissions": merged, "notes": notes})

    @app.post("/account/classroom/info")
    def classroom_info(request: Request):
        """Numbers about this account's Classroom for the Profile page: how many classes, as
        teacher and as student, and what Ragra has found in them. Read-only. CSRF-checked by
        the middleware."""
        from fastapi.responses import JSONResponse

        from ragra.adapters import user_clients
        from ragra.config import load_config

        now_iso = utc_iso(datetime.now(timezone.utc))
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            try:
                client = user_clients.classroom_client_for(conn, load_config(), user_id=user_id)
            except user_clients.UserCredentialsUnavailable:
                return JSONResponse({"ok": False, "message": "Classroom isn't connected yet."})
            except Exception as exc:  # noqa: BLE001 - report, never crash the page
                return JSONResponse({"ok": False, "message": f"Couldn't start: {str(exc)[:140]}"})

            # What Ragra has already found in this account's classes (local data, no Google call).
            rows = {
                r["source_type"]: r
                for r in conn.execute(
                    """SELECT source_type, COUNT(*) AS n,
                              SUM(CASE WHEN actual_deadline IS NOT NULL THEN 1 ELSE 0 END) AS dated,
                              SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) AS done
                       FROM tasks WHERE user_id = ? AND status != 'CANCELLED' GROUP BY source_type""",
                    (user_id,),
                )
            }
            upcoming = len(repo.tasks_due_between(conn, user_id=user_id, start_iso=now_iso, end_iso="9999-12-31T23:59:59+00:00"))
            overdue = len(repo.overdue_tasks(conn, user_id=user_id, now=now_iso))
            missed = repo.count_missed_tasks(conn, user_id=user_id)

        def count(kind, field="n"):
            row = rows.get(kind)
            return int(row[field] or 0) if row else 0

        try:
            courses = client.list_courses()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": f"Couldn't read your classes: {str(exc)[:140]}"})
        active = {c["id"] for c in courses if (c.get("courseState") or "ACTIVE") == "ACTIVE"}
        archived = len(courses) - len(active)

        def ids(fetch):
            try:
                return fetch() & active
            except Exception:  # noqa: BLE001 - one role lookup failing must not hide the rest
                return None

        teacher, student = ids(client.list_taught_course_ids), ids(client.list_student_course_ids)
        known = (teacher or set()) | (student or set())
        return JSONResponse({
            "ok": True,
            "classes": {
                "active": len(active), "archived": archived,
                "teacher": None if teacher is None else len(teacher),
                "student": None if student is None else len(student),
                "other": None if (teacher is None or student is None) else len(active - known),
            },
            "work": {
                "assignments": count("coursework"), "with_deadline": count("coursework", "dated"),
                "completed": count("coursework", "done"), "upcoming": upcoming, "overdue": overdue, "missed": missed,
                "announcements": count("announcement"), "materials": count("material"),
            },
        })

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

    # "/" is the chat-first home; "/overview" keeps the full planner page
    # (stats, tabs, folds) that used to be the home page.
    @app.get("/")
    @app.get("/overview")
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
            is_home = request.url.path == "/"
            chat_rows = []
            if is_home:
                if not repo.chat_messages(conn, user_id=user_id, limit=1):
                    repo.add_chat_message(conn, user_id=user_id, role="bot", text=chat_module.HELP_TEXT)
                chat_rows = repo.chat_messages(conn, user_id=user_id, limit=8)

        priority = due_today[0] if due_today else (overdue[0] if overdue else (due_soon[0] if due_soon else None))
        # The home page's "Upcoming Deadlines": everything open, soonest first.
        upcoming = sorted(
            [*overdue, *due_today, *due_soon],
            key=lambda t: t["actual_deadline"] or "9999",
        )[:6]
        upcoming_total = len(overdue) + len(due_today) + len(due_soon)

        # The single nearest deadline for the home card: the next one coming
        # up, or (if nothing is coming) the oldest overdue one.
        nearest = None
        pick = next(iter(sorted([*due_today, *due_soon], key=lambda t: t["actual_deadline"] or "9999")), None)
        pick = pick or (overdue[0] if overdue else None)
        if is_home and pick is not None and pick["actual_deadline"]:
            nearest = _deadline_card(pick, now)

        return templates.TemplateResponse(
            request,
            "home.html" if is_home else "today.html",
            {
                "chat_rows": chat_rows,
                "nearest": nearest,
                "has_open_alert": any(r["role"] == "alert" and r["acked_at"] is None for r in chat_rows),
                "upcoming": upcoming,
                "upcoming_total": upcoming_total,
                "overdue_ids": {t["id"] for t in overdue},
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

    # --- Deadlines: every open task with a due date, soonest first ---------
    @app.get("/deadlines")
    def deadlines_page(request: Request):
        now = datetime.now(timezone.utc)
        now_iso = utc_iso(now)
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            overdue = repo.overdue_tasks(conn, user_id=user_id, now=now_iso)
            upcoming = repo.tasks_due_between(
                conn, user_id=user_id, start_iso=now_iso, end_iso="9999-12-31T23:59:59+00:00"
            )
            sync_problem = _classroom_sync_problem(conn, user_id)
        cards = [_deadline_card(t, now) for t in [*overdue, *upcoming] if t["actual_deadline"]]
        order = [
            ("overdue", "Overdue"), ("today", "Today"), ("tomorrow", "Tomorrow"),
            ("week", "This week"), ("later", "Later"),
        ]
        groups = [
            {"key": key, "label": label, "items": [c for c in cards if c["group"] == key]}
            for key, label in order
        ]
        # Time-left filters (cumulative) and the "due within 3 hours" warning.
        def within(hours):
            return [c for c in cards if 0 <= c["hours_left"] <= hours]

        return templates.TemplateResponse(
            request, "deadlines.html",
            {"groups": [g for g in groups if g["items"]], "total": len(cards),
             "counts": {g["key"]: len(g["items"]) for g in groups},
             "within": {"h24": len(within(24)), "d7": len(within(24 * 7)), "w1": len(within(24 * 7))},
             "urgent": within(3), "sync_problem": sync_problem},
        )

    def _is_owner(conn, user_id: int) -> bool:
        owner = getattr(settings, "owner_email", None)
        row = repo.get_user(conn, user_id=user_id)
        return bool(owner and row is not None and (row["email"] or "").strip().lower() == owner)

    # --- One assignment: the same clean page from Deadlines, Tasks, chat links and reminders ----
    _REMINDER_LABELS = {
        "NEW_ASSIGNMENT": "New assignment alert", "T_MINUS_7D": "1 week before", "T_MINUS_5D": "5 days before",
        "T_MINUS_3D": "3 days before", "T_MINUS_2D": "2 days before", "T_MINUS_1D": "1 day before",
        "DUE_TODAY": "Due-today alert", "FEW_HOURS": "A few hours before", "MIDPOINT": "Halfway reminder",
        "FINAL_1H": "1 hour before", "DEADLINE_REACHED": "Deadline reached", "DEADLINE_CHANGED": "Deadline changed",
    }
    _STATUS_LABELS = {"ACTION_REQUIRED": "Not done yet", "PLANNED": "Planned", "DISCOVERED": "New", "COMPLETED": "Done", "MISSED": "Missed", "CANCELLED": "Cancelled"}
    _FIELD_LABELS = {"actual_deadline": "Deadline", "personal_deadline": "Your own target", "status": "Status",
                     "title": "Title", "description": "Details"}

    def _task_page(request: Request, task_id: int, *, back: tuple[str, str]):
        now = datetime.now(timezone.utc)
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            # Scoped lookup: someone else's task id is a plain 404, not a disclosure.
            task = repo.get_task_by_id(conn, user_id=user_id, task_id=task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            reminders = repo.reminders_for_task(conn, user_id=user_id, task_id=task_id)
            history = repo.history_for_task(conn, user_id=user_id, task_id=task_id)
        card = _deadline_card(task, now) if task["actual_deadline"] else None

        def nice(field, value):
            if value in (None, ""):
                return "none"
            return _STATUS_LABELS.get(value, value) if field == "status" else _friendly_when(value)

        reminder_rows = [{
            "label": _REMINDER_LABELS.get(r["reminder_type"], str(r["reminder_type"]).replace("_", " ").capitalize()),
            "state": {"SENT": "Sent", "PENDING": "Scheduled", "CANCELLED": "Cancelled", "FAILED": "Failed"}.get(r["status"], r["status"]),
            "tone": {"SENT": "ok", "PENDING": "wait", "FAILED": "bad"}.get(r["status"], "off"),
            "when": _friendly_when(r["sent_at"] if r["status"] == "SENT" and r["sent_at"] else r["scheduled_for"]),
        } for r in reminders]
        history_rows = [{
            "label": _FIELD_LABELS.get(h["field"], str(h["field"]).replace("_", " ").capitalize()),
            "old": nice(h["field"], h["old_value"]), "new": nice(h["field"], h["new_value"]),
            "when": _friendly_when(h["changed_at"]),
        } for h in history]
        finished = (_friendly_when(task["completed_at"]) if task["completed_at"] else
                    _friendly_when(task["missed_at"]) if task["missed_at"] else "")
        return templates.TemplateResponse(
            request, "deadline_detail.html",
            {"task": task, "card": card, "reminders": reminder_rows, "history": history_rows,
             "status_label": _STATUS_LABELS.get(task["status"], str(task["status"]).replace("_", " ").capitalize()),
             "finished": finished, "back_url": back[0], "back_label": back[1]},
        )

    @app.get("/deadlines/{task_id}")
    def deadline_detail(request: Request, task_id: int):
        return _task_page(request, task_id, back=("/deadlines", "Back to Deadlines"))

    # --- Contact us: the team, and a box for feedback and suggestions ----------
    @app.get("/contact")
    def contact_page(request: Request, sent: int = 0, error: str = ""):
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            is_owner = _is_owner(conn, user_id)
        return templates.TemplateResponse(
            request, "contact.html",
            {"sent": bool(sent), "error": error[:120], "is_owner": is_owner, "kinds": repo.FEEDBACK_KINDS},
        )

    @app.post("/contact/feedback")
    def contact_feedback(
        request: Request, kind: str = Form("suggestion"), rating: str = Form(""), message: str = Form(""),
    ):
        message = message.strip()[:2000]
        if kind not in repo.FEEDBACK_KINDS:
            kind = "suggestion"
        stars = int(rating) if rating.isdigit() and 1 <= int(rating) <= 5 else None
        if len(message) < 5:
            return RedirectResponse("/contact?error=Please+write+a+few+words+first.", status_code=303)
        since = utc_iso(datetime.now(timezone.utc) - timedelta(hours=1))
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            if repo.recent_feedback_count(conn, user_id=user_id, since_iso=since) >= repo.FEEDBACK_MAX_PER_HOUR:
                return RedirectResponse("/contact?error=That+is+a+lot+of+messages+in+an+hour.+Please+try+again+later.", status_code=303)
            repo.add_feedback(conn, user_id=user_id, kind=kind, rating=stars, message=message)
        return RedirectResponse("/contact?sent=1", status_code=303)

    @app.get("/contact/inbox")
    def contact_inbox(request: Request):
        """Everything people have sent. Only the owner account may open it; for
        anyone else it is indistinguishable from a page that does not exist."""
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            if not _is_owner(conn, user_id):
                raise HTTPException(status_code=404, detail="Not found")
            rows = repo.all_feedback(conn)
        items = [{
            "kind": r["kind"], "rating": r["rating"], "message": r["message"], "when": _friendly_when(r["created_at"]),
            "who": r["display_name"] or r["email"] or "Someone", "email": r["email"],
        } for r in rows]
        return templates.TemplateResponse(request, "contact_inbox.html", {"items": items})

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
        return _task_page(request, task_id, back=("/", "Back to Home"))

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

    # --- In-app Agent chat ---------------------------------------------------
    #
    # One thread per account. The person's messages and the agent's replies
    # use the same answerer as WhatsApp/Telegram (ragra/chat.py); alerts the
    # reminder dispatcher sends (announcements, assignments, deadline changes)
    # are appended to the same thread with role "alert".

    def _chat_json(rows) -> list[dict]:
        return [
            {"id": r["id"], "role": r["role"], "text": r["text"], "link": r["link_path"],
             "at": r["created_at"], "acked": r["acked_at"] is not None}
            for r in rows
        ]

    @app.get("/chat")
    def chat_page(request: Request):
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            if not repo.chat_messages(conn, user_id=user_id, limit=1):
                repo.add_chat_message(conn, user_id=user_id, role="bot", text=chat_module.HELP_TEXT)
            rows = repo.chat_messages(conn, user_id=user_id)
        return templates.TemplateResponse(request, "chat.html", {"messages": rows})

    @app.get("/chat/messages")
    def chat_poll(request: Request, after: int = 0):
        from fastapi.responses import JSONResponse

        with connect_closing(db_path) as conn:
            rows = repo.chat_messages(conn, user_id=_acting_user(conn, request), after_id=max(after, 0))
        return JSONResponse({"messages": _chat_json(rows)})

    @app.post("/chat/send")
    def chat_send(request: Request, text: str = Form("")):
        from fastapi.responses import JSONResponse
        from ragra.config import load_config

        text = text.strip()[:500]
        wants_json = "application/json" in request.headers.get("accept", "")
        if not text:
            return JSONResponse({"messages": []}) if wants_json else RedirectResponse("/chat", status_code=303)
        config = load_config()
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            mine = repo.add_chat_message(conn, user_id=user_id, role="user", text=text)
            try:
                reply = chat_module.answer(conn, config, user_id=user_id, text=text)
            except Exception:  # noqa: BLE001 - the thread must never break on one bad answer
                reply = "Sorry, something went wrong. Try 'pending', 'today' or 'overdue'."
            repo.add_chat_message(conn, user_id=user_id, role="bot", text=reply)
            rows = repo.chat_messages(conn, user_id=user_id, after_id=mine - 1)
        if wants_json:
            return JSONResponse({"messages": _chat_json(rows)})
        return RedirectResponse("/chat", status_code=303)

    @app.post("/chat/{message_id}/ack")
    def chat_ack(request: Request, message_id: int):
        from fastapi.responses import JSONResponse

        with connect_closing(db_path) as conn:
            repo.ack_chat_message(conn, user_id=_acting_user(conn, request), message_id=message_id)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": True})
        return RedirectResponse("/chat", status_code=303)

    @app.post("/chat/ack-all")
    def chat_ack_all(request: Request):
        with connect_closing(db_path) as conn:
            repo.ack_all_chat_alerts(conn, user_id=_acting_user(conn, request))
        return RedirectResponse("/chat", status_code=303)

    @app.post("/chat/clear")
    def chat_clear(request: Request):
        with connect_closing(db_path) as conn:
            repo.clear_chat(conn, user_id=_acting_user(conn, request))
        return RedirectResponse("/chat", status_code=303)

    # --- WhatsApp webhook (Meta Cloud API) ---------------------------------

    @app.get("/whatsapp/webhook")
    def whatsapp_verify(request: Request):
        """Meta's one-off handshake when the webhook URL is registered: echo the
        challenge back only if the verify token matches the one we hold."""
        from ragra.config import load_config

        params = request.query_params
        expected = load_config().whatsapp_verify_token
        token = params.get("hub.verify_token") or ""
        if (
            params.get("hub.mode") == "subscribe"
            and expected
            and hmac.compare_digest(token, expected)
        ):
            return PlainTextResponse(params.get("hub.challenge") or "")
        return PlainTextResponse("Forbidden", status_code=403)

    @app.post("/whatsapp/webhook")
    async def whatsapp_inbound(request: Request, background: BackgroundTasks):
        from ragra.config import load_config

        body = await request.body()
        if not whatsapp_web.signature_is_valid(
            body=body,
            header=request.headers.get("x-hub-signature-256"),
            secret=load_config().whatsapp_app_secret,
        ):
            return PlainTextResponse("Invalid signature", status_code=403)
        for number, text in whatsapp_web.extract_text_messages(whatsapp_web.parse_payload(body)):
            background.add_task(whatsapp_web.reply_to_whatsapp, db_path, number, text)
        # Always 200 once the signature is good: Meta retries anything else.
        return PlainTextResponse("ok")

    # --- Telegram bot ------------------------------------------------------

    @app.post("/telegram/webhook")
    async def telegram_inbound(request: Request, background: BackgroundTasks):
        from ragra.config import load_config

        config = load_config()
        if not telegram_web.secret_is_valid(
            header=request.headers.get("x-telegram-bot-api-secret-token"),
            bot_token=config.telegram_bot_token,
        ):
            return PlainTextResponse("Forbidden", status_code=403)
        parsed = telegram_web.parse_update(await request.body())
        if parsed:
            background.add_task(telegram_web.reply_to_telegram, db_path, parsed[0], parsed[1])
        return PlainTextResponse("ok")  # always 200 once authentic: Telegram retries anything else

    @app.post("/account/telegram/link")
    def telegram_link(request: Request):
        from ragra.config import load_config

        config = load_config()
        if not (config.telegram_bot_token and config.telegram_bot_username):
            return RedirectResponse("/account?saved=notelegram", status_code=303)
        with connect_closing(db_path) as conn:
            code = repo.create_telegram_link_code(conn, user_id=_acting_user(conn, request))
        return RedirectResponse(
            f"https://t.me/{config.telegram_bot_username}?start={code}", status_code=303
        )

    @app.post("/account/telegram/disconnect")
    def telegram_disconnect(request: Request):
        with connect_closing(db_path) as conn:
            repo.disconnect_telegram_chat(conn, user_id=_acting_user(conn, request))
        return RedirectResponse("/account?saved=saved", status_code=303)

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
        from ragra.config import load_config

        _wa_config = load_config()
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            user_row = repo.get_user(conn, user_id=user_id)
            profile = load_raw_profile(conn, user_id=user_id)
            preferences = load_preferences(conn, user_id=user_id)
            health_rows = {
                row["service"]: row for row in repo.list_credential_health(conn, user_id=user_id)
            }
            # A service with no credential_health row yet (never attempted
            # this tick cycle) is exactly NOT_CONNECTED - the same state a
            # fresh account starts in, not an error.
            google_status = {
                service: {
                    "state": (health_rows.get(service)["state"] if service in health_rows
                              else credential_health.CredentialState.NOT_CONNECTED.value),
                    "detail": health_rows.get(service)["last_error_detail"] if service in health_rows else None,
                    "last_success_at": health_rows.get(service)["last_success_at"] if service in health_rows else None,
                }
                for service in google_reconnect.SERVICES
            }
            sync_problem = _classroom_sync_problem(conn, user_id)
            permissions = google_reconnect.permission_rows(conn, user_id=user_id)

        return templates.TemplateResponse(
            request,
            "account.html",
            {
                "display_name": user_row["display_name"] if user_row else None,
                "account_email": user_row["email"] if user_row else None,
                "account_picture": user_row["picture_url"] if user_row else None,
                "profile": profile,
                "enrollment_text": _enrollment_to_text(profile.enrollment),
                "preferences": preferences,
                "google_status": google_status,
                "sync_problem": sync_problem,
                "permissions": permissions,
                "telegram_ready": bool(_wa_config.telegram_bot_token and _wa_config.telegram_bot_username),
                "whatsapp_ready": bool(_wa_config.whatsapp_token and _wa_config.whatsapp_phone_number_id),
                "whatsapp_bot_number": _wa_config.whatsapp_display_number,
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
        background: BackgroundTasks,
        email_enabled: str = Form(""),
        email_to: str = Form(""),
        hermes_enabled: str = Form(""),
        hermes_target: str = Form(""),
        email_change: str = Form(""),
        hermes_change: str = Form(""),
        whatsapp_enabled: str = Form(""),
        whatsapp_to: str = Form(""),
        whatsapp_change: str = Form(""),
        telegram_enabled: str = Form(""),
    ):
        # A checkbox's value is present only when checked, so "enabled" is
        # derived from presence - but the destination itself is saved
        # regardless of the toggle, so switching a channel off never throws
        # away the address it will be switched back on with.
        # Once an address is saved the page shows it as saved and offers
        # "Change"; the new value arrives in *_change and wins over the saved one.
        clean_email = email_change.strip() or email_to.strip() or None
        clean_hermes = hermes_change.strip() or hermes_target.strip() or None
        typed_whatsapp = whatsapp_change.strip() or whatsapp_to.strip()
        clean_whatsapp = normalize_whatsapp_number(typed_whatsapp)
        bad_number = bool(typed_whatsapp) and clean_whatsapp is None
        preferences = NotificationPreferences(
            email_enabled=bool(email_enabled) and clean_email is not None,
            email_to=clean_email,
            hermes_enabled=bool(hermes_enabled) and clean_hermes is not None,
            hermes_target=clean_hermes,
            whatsapp_enabled=bool(whatsapp_enabled) and clean_whatsapp is not None,
            whatsapp_to=clean_whatsapp,
        )
        welcome: list = []
        wanted: list = []
        with connect_closing(db_path) as conn:
            user_id = _acting_user(conn, request)
            before = load_preferences(conn, user_id=user_id)
            # The Telegram chat is only ever set by the link handshake with the
            # bot - never from this form - so only the on/off switch is read.
            preferences = replace(
                preferences,
                telegram_chat_id=before.telegram_chat_id,
                telegram_enabled=bool(telegram_enabled) and before.telegram_chat_id is not None,
            )
            if bad_number:
                # A number that cannot be a phone number is refused; whatever
                # was saved before stays exactly as it was.
                preferences = replace(
                    preferences, whatsapp_enabled=before.whatsapp_enabled, whatsapp_to=before.whatsapp_to
                )
            save_preferences(conn, user_id=user_id, preferences=preferences)
            # Tell the person their channel works: a welcome message to each
            # channel that was just switched on or pointed somewhere new. It is
            # sent after the response (a slow messaging service must not stall
            # the page) and a failure to build or send it never undoes the save.
            try:
                from ragra.config import load_config

                wanted = channels_needing_welcome(before, preferences)
                welcome = welcome_providers(
                    conn, load_config(), user_id=user_id, channels=wanted
                )
            except Exception:  # noqa: BLE001 - the welcome is a courtesy, not part of saving
                welcome = []
            if welcome:
                # The same welcome shows in the in-app chat as an unread alert.
                note = welcome_notification()
                repo.add_chat_message(
                    conn, user_id=user_id, role="alert", text=note.text, link_path=note.link_path
                )
        if welcome:
            background.add_task(send_to_all_providers, welcome, welcome_notification())
        # What the page tells the person: saved, saved + welcome on its way, or
        # saved but the server has no way to send yet.
        outcome = "badnumber" if bad_number else ("welcome" if welcome else ("nosend" if wanted else "saved"))
        return RedirectResponse(f"/account?saved={outcome}", status_code=303)

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
