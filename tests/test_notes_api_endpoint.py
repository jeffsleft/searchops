"""
Tests for the Notes API (Claude Desktop MCP server): GET /api/notes/jobs/search,
POST /api/notes/jobs/{job_id}/notes, GET /api/notes/jobs/{job_id}/notes.

Covers the auth contract specifically: these routes are bearer-authed against
NOTES_API_TOKEN, a *different* secret from the /api/sync/ prefix's APP_PASSWORD
(app/auth.py BEARER_AUTH_PREFIXES) — so a token mix-up must fail closed, not
fall back to authenticating with the other app's token.
"""
import os
import tempfile

os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("NOTES_API_TOKEN", "test-notes-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest
from starlette.testclient import TestClient

import app.config as config
import app.models as models

config.DATABASE_PATH = _TMP_DB
models.DATABASE_PATH = _TMP_DB

from app.models import init_db, get_db
from app.routes import create_app

AUTH_HEADERS = {
    "Authorization": "Bearer test-notes-token",
    "X-Requested-With": "XMLHttpRequest",
}


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture(scope="module")
def client():
    app = create_app()
    return TestClient(app)


def _insert_job(conn, company="TestCorp", job_title="RevOps Lead"):
    cursor = conn.execute(
        """INSERT INTO jobs (company, job_title, status, pipeline_stage, date_found)
           VALUES (?, ?, ?, ?, ?)""",
        (company, job_title, "Identified", "identified", "2026-08-01"),
    )
    return cursor.lastrowid


# --- auth --------------------------------------------------------------------

class TestNotesApiAuth:
    def test_no_token_rejected_with_401(self, client):
        resp = client.get("/api/notes/jobs/search", params={"q": "Acme"})
        assert resp.status_code == 401

    def test_wrong_token_rejected_with_401(self, client):
        resp = client.get(
            "/api/notes/jobs/search",
            params={"q": "Acme"},
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert resp.status_code == 401

    def test_app_password_does_not_authenticate_notes_prefix(self, client):
        """The /api/sync/ token (APP_PASSWORD) must not work here — the two
        prefixes are deliberately separate secrets."""
        resp = client.get(
            "/api/notes/jobs/search",
            params={"q": "Acme"},
            headers={"Authorization": "Bearer test-password"},
        )
        assert resp.status_code == 401

    def test_notes_token_does_not_authenticate_sync_prefix(self, client):
        resp = client.post(
            "/api/sync/interview-session",
            json={"company": "X", "date": "2026-08-07", "mode": "prep"},
            headers={"Authorization": "Bearer test-notes-token", "X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 401

    def test_valid_token_authenticates(self, client):
        resp = client.get("/api/notes/jobs/search", params={"q": "Acme"}, headers=AUTH_HEADERS)
        assert resp.status_code == 200


# --- search --------------------------------------------------------------

def test_search_returns_matches(client):
    with get_db() as conn:
        job_id = _insert_job(conn, company="Synthesia", job_title="Director of RevOps")
    resp = client.get("/api/notes/jobs/search", params={"q": "Synthesia"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert any(m["job_id"] == job_id for m in body["matches"])


def test_search_unknown_query_returns_empty_matches(client):
    resp = client.get(
        "/api/notes/jobs/search", params={"q": "Zzyzx Nonexistent Q9"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["matches"] == []


# --- add note --------------------------------------------------------------

def test_add_note_success(client):
    with get_db() as conn:
        job_id = _insert_job(conn, company="AddNoteCo")
    resp = client.post(
        f"/api/notes/jobs/{job_id}/notes",
        json={"text": "Recruiter screen went well.", "source": "claude-desktop"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    with get_db() as conn:
        row = conn.execute(
            "SELECT text, source FROM job_notes WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    assert row["text"] == "Recruiter screen went well."
    assert row["source"] == "claude-desktop"


def test_add_note_requires_csrf_header(client):
    with get_db() as conn:
        job_id = _insert_job(conn, company="NoCsrfCo")
    resp = client.post(
        f"/api/notes/jobs/{job_id}/notes",
        json={"text": "should be rejected"},
        headers={"Authorization": "Bearer test-notes-token"},  # no X-Requested-With
    )
    assert resp.status_code == 403


def test_add_note_unknown_job_id_404_no_write(client):
    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0]
    resp = client.post(
        "/api/notes/jobs/999999/notes",
        json={"text": "orphan note"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404
    with get_db() as conn:
        after = conn.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0]
    assert after == before


def test_add_note_empty_text_400(client):
    with get_db() as conn:
        job_id = _insert_job(conn, company="EmptyNoteCo")
    resp = client.post(
        f"/api/notes/jobs/{job_id}/notes", json={"text": "  "}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 400


def test_add_note_too_long_400(client):
    with get_db() as conn:
        job_id = _insert_job(conn, company="LongNoteCo")
    resp = client.post(
        f"/api/notes/jobs/{job_id}/notes", json={"text": "x" * 5001}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 400


# --- recent notes ------------------------------------------------------------

def test_recent_notes_returns_history_in_order(client):
    with get_db() as conn:
        job_id = _insert_job(conn, company="RecentNotesCo")
    for text in ("first", "second", "third"):
        client.post(
            f"/api/notes/jobs/{job_id}/notes", json={"text": text}, headers=AUTH_HEADERS
        )
    resp = client.get(f"/api/notes/jobs/{job_id}/notes", params={"limit": 5}, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert [n["text"] for n in body["notes"]] == ["third", "second", "first"]


def test_recent_notes_unknown_job_404(client):
    resp = client.get("/api/notes/jobs/999999/notes", headers=AUTH_HEADERS)
    assert resp.status_code == 404


def test_recent_notes_negative_limit_does_not_return_unbounded_history(client):
    """SQLite treats LIMIT <= 0 as unlimited — a negative/zero caller-supplied
    limit must not bypass the page size (see get_recent_notes clamping)."""
    with get_db() as conn:
        job_id = _insert_job(conn, company="NegativeLimitCo")
    for i in range(10):
        client.post(f"/api/notes/jobs/{job_id}/notes", json={"text": f"note {i}"}, headers=AUTH_HEADERS)

    resp = client.get(f"/api/notes/jobs/{job_id}/notes", params={"limit": -1}, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()["notes"]) <= 50
