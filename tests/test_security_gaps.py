"""Security fixes from the 2026-09-24 review: login lockout that can't be
dodged with fake X-Forwarded-For addresses, 7-day sessions, and a cap on
AI-heavy requests."""
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import MAX_AGE, SESSION_COOKIE
from app.models import get_db
from app.routes import _client_ip, create_app
from app.security.rate_limit import AIRateLimitMiddleware, is_ai_request


@pytest.fixture(autouse=True)
def _clear_attempts():
    with get_db() as conn:
        conn.execute("DELETE FROM login_attempts")
    yield
    with get_db() as conn:
        conn.execute("DELETE FROM login_attempts")


def _req(xff=None):
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 1)})


def test_client_ip_trusts_the_proxy_appended_entry_not_the_callers_claim():
    assert _client_ip(_req("6.6.6.6, 203.0.113.7")) == "203.0.113.7"
    assert _client_ip(_req()) == "10.0.0.9"


def test_rotating_fake_addresses_still_hits_the_global_lockout():
    client = TestClient(create_app())
    for i in range(25):
        client.post("/login", data={"password": "wrong"}, headers={"X-Forwarded-For": f"1.1.1.{i}"})
    r = client.post("/login", data={"password": "wrong"}, headers={"X-Forwarded-For": "9.9.9.9"})
    assert "Too many attempts" in r.text


def test_sessions_last_seven_days():
    assert MAX_AGE == 7 * 24 * 3600
    import os
    client = TestClient(create_app())
    r = client.post("/login", data={"password": os.environ["APP_PASSWORD"]}, follow_redirects=False)
    assert r.status_code == 302
    cookie = r.headers["set-cookie"]
    assert SESSION_COOKIE in cookie and f"Max-Age={MAX_AGE}" in cookie


def test_ai_paths_are_recognised():
    assert is_ai_request("POST", "/job/score")
    assert is_ai_request("POST", "/job/12/cover-letter")
    assert is_ai_request("POST", "/prep/sessions/3/draft")
    assert not is_ai_request("GET", "/job/12/cover-letter")
    assert not is_ai_request("POST", "/job/12/notes")


def test_ai_budget_returns_429_with_a_reason_once_spent():
    async def ok(request):
        return PlainTextResponse("ok")
    app = Starlette(routes=[Route("/job/score", ok, methods=["POST"]),
                            Route("/job/1/notes", ok, methods=["POST"])])
    app.add_middleware(AIRateLimitMiddleware, limit=2)
    c = TestClient(app)
    assert [c.post("/job/score").status_code for _ in range(3)] == [200, 200, 429]
    assert c.post("/job/score").headers["X-Save-Status"] == "failed"
    assert c.post("/job/1/notes").status_code == 200  # ordinary saves are never capped
