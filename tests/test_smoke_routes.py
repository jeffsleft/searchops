"""
Integration smoke test for the web routes.

This is the safety net for refactoring routes.py: it boots the real Starlette
app against a temp SQLite DB and hits every no-parameter GET route as an
authenticated user, asserting none of them 500. It won't catch behavioural
regressions (response *content*), but it reliably catches the failure mode that
matters during a service-layer extraction: a broken import, a renamed helper, a
missing variable — anything that turns a working handler into a 500.

Env must be set BEFORE importing app modules (app.config / app.auth read it at
import time), so the os.environ block sits at the very top of this module.
"""
import os
import tempfile

# --- environment must exist before any app import ---------------------------
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest
from starlette.testclient import TestClient

from app.models import init_db
from app.routes import create_app
from app.auth import create_session_token, SESSION_COOKIE


# No-parameter GET routes — each exercises a handler + its template against an
# empty DB. (Routes needing a path id are excluded; they're covered once data
# fixtures exist.)
NO_PARAM_GET_ROUTES = [
    "/",
    "/companies",
    "/vetting",
    "/rejected",
    "/pipeline",
    "/prep",
    "/prep/palette",
    "/followups/widget",
    "/recruiters",
    "/settings",
    "/settings/rubric",
    "/settings/methodology",
    "/settings/filters",
    "/guide",
    "/admin/patterns",
    "/discovered",
    "/targets",
    "/api/task-status",
    "/api/sync-status",
]


@pytest.fixture(scope="module")
def client():
    # DATABASE_PATH is read into module namespaces at import time, so an
    # os.environ override can lose to import order when other test files import
    # app.config first. Patch the bound names directly to guarantee the temp DB.
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = _TMP_DB
    models.DATABASE_PATH = _TMP_DB
    init_db()
    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


def test_login_page_public(client):
    # /login is public and must render without a session.
    bare = TestClient(create_app())
    resp = bare.get("/login")
    assert resp.status_code == 200


def test_unauthenticated_redirects():
    bare = TestClient(create_app())
    resp = bare.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


@pytest.mark.parametrize("path", NO_PARAM_GET_ROUTES)
def test_get_route_does_not_500(client, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code < 500, f"{path} returned {resp.status_code}"
