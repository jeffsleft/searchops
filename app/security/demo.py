"""Public portfolio demo guard (active only when DEMO_MODE is on).

- Read-only: any non-GET request gets 403 with a reason the save toast shows.
- Hidden from search engines: X-Robots-Tag on every response (plus /robots.txt).
- No login: AuthMiddleware lets everything through in demo mode.
The demo's data is fictional and rebuilt at startup, so nothing a visitor does
can persist; blocking writes keeps the tour identical for everyone.
"""
import re
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, RedirectResponse

from app.config import is_demo

READ_ONLY_MSG = "This is a read-only demo with fictional data."

# Resume and cover-letter files are assembled from the owner's real resume
# (data/resume.docx) and name, so they never render in the demo, even if that
# file were present. The Application Kit shows the tailored content instead.
_PERSONAL_DOCS = re.compile(r"^/job/\d+/(resume|cover-letter)(/download)?/?$")
_PERSONAL_DOCS_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>Not in the demo</title>"
    "<body style='font-family:system-ui;max-width:520px;margin:15vh auto;line-height:1.5;padding:0 16px'>"
    "<h1 style='font-size:20px'>Not part of the demo</h1>"
    "<p>Resumes and cover letters are built from the candidate's real documents, so the demo "
    "leaves them out. The Application Kit on each role shows the tailored bullets and hooks.</p>"
    "<p><a href='/'>Back to the demo</a></p></body>")


class DemoModeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not is_demo():
            return await call_next(request)
        if request.url.path == "/login":
            return RedirectResponse(url="/", status_code=302)
        if _PERSONAL_DOCS.match(request.url.path):
            response = HTMLResponse(_PERSONAL_DOCS_PAGE, status_code=404)
        elif request.method not in ("GET", "HEAD", "OPTIONS"):
            response = HTMLResponse(READ_ONLY_MSG, status_code=403,
                                    headers={"X-Save-Status": "failed", "X-Save-Message": quote(READ_ONLY_MSG)})
        else:
            response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
