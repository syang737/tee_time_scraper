"""Single shared password gating the GUI, via a signed session cookie."""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="teetimes-session")


def is_configured() -> bool:
    """Whether a GUI password exists to check against at all.

    Kept separate from check_password so a server with no password set can
    say so, instead of reporting every attempt as a wrong password and
    leaving you to wonder which of the two it is.
    """
    return bool(config.GUI_PASSWORD)


def check_password(candidate: str) -> bool:
    """Constant-time comparison so the password can't be timed out of us."""
    if not is_configured():
        return False
    return hmac.compare_digest(candidate, config.GUI_PASSWORD)


def issue_cookie(response) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        _serializer.dumps("ok"),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Off by default so local http development works; set COOKIE_SECURE
        # in .env once the GUI is served over HTTPS.
        secure=config.COOKIE_SECURE,
    )


def clear_cookie(response) -> None:
    response.delete_cookie(config.SESSION_COOKIE)


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return True


def require_login(request: Request) -> RedirectResponse | None:
    """Return a redirect when the request isn't authenticated, else None."""
    if is_logged_in(request):
        return None
    return RedirectResponse("/login", status_code=303)
