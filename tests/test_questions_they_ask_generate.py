"""
Regression test for question_they_ask_generate.

Before 2026-07-21 this route called job.get('sector', ...) on a raw sqlite3.Row,
which has no .get() method -- every generation attempt threw AttributeError
(caught by the route's own try/except and rendered as an opaque error message).
The fix converts the row to a dict before use and, while in there, upgrades the
prompt to ground questions in the profile's anchor stories and cached company
research instead of just the bare role fields.
"""
import os
import tempfile

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


@pytest.fixture(scope="module")
def client():
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = _TMP_DB
    models.DATABASE_PATH = _TMP_DB
    init_db()
    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


def test_generate_does_not_crash_on_row_and_persists_questions(client, monkeypatch):
    """The core regression: job is a sqlite3.Row, and job.get('sector', ...) /
    job.get('recommended_angle', ...) must not raise AttributeError."""
    from app.models import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO jobs (company, job_title, sector, pipeline_stage,
                                 recommended_angle, status, date_found, discovery_source)
               VALUES (?, ?, ?, 'identified', ?, 'Identified', '2026-07-21', 'manual')""",
            ("Forgeline", "Director of RevOps", "DevTools", "Own the GTM systems build"),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO interview_sessions (job_id, type_id, label, position, created_at, updated_at)
               VALUES (?, 'recruiter', 'Recruiter Screen', 0, '2026-07-21T00:00:00+00:00', '2026-07-21T00:00:00+00:00')""",
            (job_id,),
        )
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    fake_questions = [
        "How do you measure RevOps maturity today?",
        "What does the greenfield build actually look like here?",
    ]

    class FakeProvider:
        def generate(self, prompt):
            # Sanity: the upgraded prompt should carry sector/angle through.
            assert "DevTools" in prompt
            assert "Own the GTM systems build" in prompt
            import json
            return json.dumps(fake_questions)

    monkeypatch.setattr("app.routes_prep.get_provider", lambda: FakeProvider())

    resp = client.post(
        f"/prep/sessions/{session_id}/questions-they-ask/generate",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200, resp.text
    assert "AttributeError" not in resp.text
    assert "Error generating questions" not in resp.text

    with get_db() as conn:
        rows = conn.execute(
            "SELECT prompt FROM session_questions_they_ask WHERE session_id = ? ORDER BY position",
            (session_id,),
        ).fetchall()
    persisted = [r["prompt"] for r in rows]
    assert persisted == fake_questions


def test_generate_handles_missing_sector_and_angle(client, monkeypatch):
    """A job with no sector/recommended_angle set (both NULL) must not crash --
    this is the exact shape that triggered the original bug in production."""
    from app.models import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO jobs (company, job_title, pipeline_stage, status,
                                 date_found, discovery_source)
               VALUES (?, ?, 'identified', 'Identified', '2026-07-21', 'manual')""",
            ("BareBones Inc", "VP Ops"),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO interview_sessions (job_id, type_id, label, position, created_at, updated_at)
               VALUES (?, 'hm', 'HM Interview', 0, '2026-07-21T00:00:00+00:00', '2026-07-21T00:00:00+00:00')""",
            (job_id,),
        )
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    class FakeProvider:
        def generate(self, prompt):
            import json
            return json.dumps(["A question?"])

    monkeypatch.setattr("app.routes_prep.get_provider", lambda: FakeProvider())

    resp = client.post(
        f"/prep/sessions/{session_id}/questions-they-ask/generate",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200, resp.text
    assert "AttributeError" not in resp.text
