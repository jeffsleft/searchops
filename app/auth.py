import os
import logging
import secrets as _secrets
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, HTMLResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

_jinja = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)

SESSION_SECRET = os.environ["SESSION_SECRET"]  # Hard fail if not set — add to recruiting-secrets Modal Secret
if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        f"SESSION_SECRET must be at least 32 characters (got {len(SESSION_SECRET)}). "
        "Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )

SECRET_KEY = SESSION_SECRET
SESSION_COOKIE = "re_session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_serializer = URLSafeTimedSerializer(SECRET_KEY)

PUBLIC_PATHS = {"/login", "/favicon.ico"}

# Paths callable by non-browser clients (e.g. the interview-scorecard skill
# running in Claude Desktop/Cowork) via `Authorization: Bearer <APP_PASSWORD>`
# instead of a session cookie. Kept to a narrow prefix, not app-wide, to limit
# blast radius — reuses the existing APP_PASSWORD secret rather than minting a
# new one (Modal's `secret create` has no additive "add one key" mode, so
# introducing a fresh secret risks clobbering recruiting-secrets' other keys).
BEARER_AUTH_PREFIXES = ("/api/sync/",)


def create_session_token() -> str:
    return _serializer.dumps("authenticated")


def verify_session_token(token: str) -> bool:
    try:
        _serializer.loads(token, max_age=MAX_AGE)
        return True
    except BadSignature:
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/static"):
            return await call_next(request)

        if request.url.path.startswith(BEARER_AUTH_PREFIXES):
            from app.config import APP_PASSWORD
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer ") and APP_PASSWORD:
                presented = auth_header[len("Bearer "):]
                if _secrets.compare_digest(presented, APP_PASSWORD):
                    return await call_next(request)
            # Bearer auth was attempted (or path requires it) but failed —
            # don't fall through to a redirect; a non-browser client can't
            # follow it. Return a plain 401.
            token = request.cookies.get(SESSION_COOKIE)
            if token and verify_session_token(token):
                return await call_next(request)
            return HTMLResponse("Unauthorized", status_code=401)

        token = request.cookies.get(SESSION_COOKIE)
        if not token or not verify_session_token(token):
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)


def login_page(error: str = "") -> HTMLResponse:
    html = _jinja.get_template("login.html").render(error=error)
    return HTMLResponse(html)
