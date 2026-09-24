"""Cap on AI-heavy requests, so a stolen session can't run up the Gemini bill.

Every matching POST (scoring, cover letters, briefs, research, prep generation,
scans) counts against one rolling hourly budget for the whole app. In-memory
is enough: the app runs as exactly one container (app/main.py), and a restart
resetting the count is harmless. A batch action (rescore-all, research-batch)
counts once, so the budget sits far above a heavy day of real use.
"""
import re
import threading
import time
from collections import deque
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse

AI_REQUESTS_PER_HOUR = 60
_WINDOW_S = 3600

AI_PATHS = [re.compile(p) for p in (
    r"^/job/score$",
    r"^/job/\d+/(cover-letter|generate-brief|research|fetch-and-score|paste-and-score|rescore)$",
    r"^/companies/(\d+/research|research-batch)$",
    r"^/targets(/\d+/(scan-now|research))?$",
    r"^/discovered/add-linkedin$",
    r"^/prep/sessions/\d+/(questions-they-ask/generate|draft|analyze)$",
    r"^/prep/backfill-themes$",
    r"^/admin/rescore-all$",
    r"^/api/discovery/scan$",
)]


def is_ai_request(method: str, path: str) -> bool:
    return method == "POST" and any(p.match(path) for p in AI_PATHS)


class AIRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit: int = AI_REQUESTS_PER_HOUR, window_s: int = _WINDOW_S):
        super().__init__(app)
        self._limit = limit
        self._window_s = window_s
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def _allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > self._window_s:
                self._hits.popleft()
            if len(self._hits) >= self._limit:
                return False
            self._hits.append(now)
            return True

    async def dispatch(self, request, call_next):
        if is_ai_request(request.method, request.url.path) and not self._allow():
            msg = f"AI limit reached ({self._limit} actions an hour). Try again later."
            return HTMLResponse(msg, status_code=429,
                                headers={"X-Save-Status": "failed", "X-Save-Message": quote(msg)})
        return await call_next(request)
