"""Cross-site request forgery protection.

The attack this stops: another site causes the user's browser to submit a
form to Ragra. The browser attaches the session cookie because cookies are
sent by origin, not by who asked - so without a second factor the request
is indistinguishable from one the user meant to make.

The token is derived from the session token rather than stored:

    csrf_token = SHA-256(session_token + "|ragra-csrf")

That is sound precisely because of what an attacker in this scenario can
and cannot do. They can make the browser send a request; they cannot read
the response, and they cannot read the session cookie, which is HttpOnly
and confined to Ragra's origin. So they cannot compute the token, and a
forged submission arrives without one. Deriving rather than storing means
there is no server secret to manage, nothing extra to persist, and no way
for a restart to invalidate every open form.

The derivation is one-way, so a CSRF token that leaks does not reveal the
session token. It is still never put in a URL: query strings end up in
history, logs, and Referer headers, and a leaked token there would be
usable to forge a request. It travels only in a form body or the
X-CSRF-Token header.

SameSite=Lax on the session cookie (see ragra/web/app.py) is the other half
of this. Neither is relied on alone: SameSite is enforcement Ragra does not
control, and a token check is enforcement that only works if every route is
covered - so this is applied as middleware over all unsafe methods rather
than route by route, and a route added tomorrow is covered without anyone
remembering to.
"""

from __future__ import annotations

import hashlib
import secrets

from ragra.web import sessions

FIELD_NAME = "csrf_token"
HEADER_NAME = "x-csrf-token"

# Methods that can change state. GET/HEAD/OPTIONS/TRACE are excluded
# because they are not supposed to change anything - a route that mutates
# state on GET is a bug this cannot compensate for.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_DERIVATION_SALT = "|ragra-csrf"


def token_for(session_token: str | None) -> str:
    """The CSRF token belonging to a session, or "" when there is none.

    An empty string is never a valid token (see `verify`), so a request
    without a session cannot accidentally satisfy the check.
    """
    if not session_token:
        return ""
    return hashlib.sha256((session_token + _DERIVATION_SALT).encode("utf-8")).hexdigest()


def verify(*, submitted: str | None, session_token: str | None) -> bool:
    """Whether a submitted token matches the session presenting it.

    Compared with `compare_digest` rather than `==`: the difference is
    unobservable here in practice, but a constant-time comparison costs
    nothing and removes the question entirely.
    """
    expected = token_for(session_token)
    if not expected or not submitted:
        return False
    return secrets.compare_digest(submitted, expected)


def session_token_from(request) -> str | None:
    return request.cookies.get(sessions.COOKIE_NAME)
