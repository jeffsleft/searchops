"""
Two things not covered elsewhere:

1. Regression: the web "My notes" box (POST /job/{id}/notes) must go through
   add_job_note() — i.e. write a job_notes history row, not just update
   jobs.notes directly — now that job_actions.add_job_note is the single
   sanctioned writer shared with the Claude-Desktop MCP server.
2. The legacy jobs.notes -> job_notes(source='legacy') backfill migration in
   app.models.init_db(): must preserve the existing note, run exactly once
   (no duplicate legacy rows on repeated init_db() calls), and must not touch
   jobs.notes itself.
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
from app.auth import create_session_token, SESSION_COOKIE

HX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def client():
    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


def _insert_job(conn, company="WebNotesCo"):
    cursor = conn.execute(
        """INSERT INTO jobs (company, job_title, status, pipeline_stage, date_found)
           VALUES (?, ?, ?, ?, ?)""",
        (company, "RevOps Lead", "Identified", "identified", "2026-08-01"),
    )
    return cursor.lastrowid


def test_web_note_box_writes_job_notes_row(client):
    with get_db() as conn:
        job_id = _insert_job(conn)

    resp = client.post(f"/job/{job_id}/notes", data={"notes": "Angle: greenfield CS Ops"}, headers=HX_HEADERS)
    assert resp.status_code == 200

    with get_db() as conn:
        rows = conn.execute(
            "SELECT text, source FROM job_notes WHERE job_id = ?", (job_id,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "Angle: greenfield CS Ops"
    assert rows[0]["source"] == "web"


def test_web_note_box_response_escapes_html_in_note_text(client):
    """Confirms Jinja autoescape actually covers components/job_notes_list.html
    — a note containing markup must come back escaped, not as live HTML/script."""
    with get_db() as conn:
        job_id = _insert_job(conn, company="XssCheckCo")

    payload = '<script>alert(1)</script> & "quoted"'
    resp = client.post(f"/job/{job_id}/notes", data={"notes": payload}, headers=HX_HEADERS)
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    assert "&amp;" in resp.text


def test_web_note_box_second_save_appends_a_second_row(client):
    with get_db() as conn:
        job_id = _insert_job(conn, company="WebNotesAppendCo")

    client.post(f"/job/{job_id}/notes", data={"notes": "first"}, headers=HX_HEADERS)
    client.post(f"/job/{job_id}/notes", data={"notes": "second"}, headers=HX_HEADERS)

    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM job_notes WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert count == 2  # append, not overwrite


class TestLegacyNotesBackfillMigration:
    """Exercises init_db()'s backfill against a *separate* DB file so it
    doesn't interfere with the module-level _db fixture's DB."""

    def _fresh_db_path(self):
        return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

    def test_backfill_preserves_existing_note(self):
        path = self._fresh_db_path()
        config.DATABASE_PATH = path
        models.DATABASE_PATH = path
        try:
            init_db()
            with get_db() as conn:
                cur = conn.execute(
                    """INSERT INTO jobs (company, job_title, status, pipeline_stage, date_found, notes)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("LegacyNotesCo", "RevOps Lead", "Identified", "identified", "2026-08-01",
                     "Pre-existing note from before job_notes existed."),
                )
                job_id = cur.lastrowid

            # Re-running init_db() is what triggers the backfill (it runs inside
            # init_db(), not on raw INSERT) — mirrors a real deploy re-running it.
            init_db()

            with get_db() as conn:
                legacy_rows = conn.execute(
                    "SELECT text, source FROM job_notes WHERE job_id = ? AND source = 'legacy'",
                    (job_id,),
                ).fetchall()
                job_row = conn.execute("SELECT notes FROM jobs WHERE id = ?", (job_id,)).fetchone()

            assert len(legacy_rows) == 1
            assert legacy_rows[0]["text"] == "Pre-existing note from before job_notes existed."
            # jobs.notes is left alone by the migration (not dropped, not cleared)
            assert job_row["notes"] == "Pre-existing note from before job_notes existed."
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_backfill_is_idempotent_across_repeated_init_db_calls(self):
        path = self._fresh_db_path()
        config.DATABASE_PATH = path
        models.DATABASE_PATH = path
        try:
            init_db()
            with get_db() as conn:
                cur = conn.execute(
                    """INSERT INTO jobs (company, job_title, status, pipeline_stage, date_found, notes)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("IdempotentCo", "RevOps Lead", "Identified", "identified", "2026-08-01", "one note"),
                )
                job_id = cur.lastrowid

            init_db()
            init_db()
            init_db()

            with get_db() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM job_notes WHERE job_id = ? AND source = 'legacy'",
                    (job_id,),
                ).fetchone()[0]
            assert count == 1  # not 3 — one legacy row per job, regardless of how many init_db() calls
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_backfill_skips_jobs_with_no_notes(self):
        path = self._fresh_db_path()
        config.DATABASE_PATH = path
        models.DATABASE_PATH = path
        try:
            init_db()
            with get_db() as conn:
                cur = conn.execute(
                    """INSERT INTO jobs (company, job_title, status, pipeline_stage, date_found)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("NoLegacyNotesCo", "RevOps Lead", "Identified", "identified", "2026-08-01"),
                )
                job_id = cur.lastrowid
            init_db()
            with get_db() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM job_notes WHERE job_id = ?", (job_id,)
                ).fetchone()[0]
            assert count == 0
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
