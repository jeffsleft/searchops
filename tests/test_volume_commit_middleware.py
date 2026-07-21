"""
Regression test for the Session 52/53 write-durability bug.

Before this fix, the `web()` ASGI process never called `volume.commit()`
anywhere in its request path. A write (e.g. a pipeline stage change) was
immediately visible to the same warm container but never flushed to durable
storage, so a fresh `modal volume get` (or a different container) could show
the pre-change state despite a 200 OK — see HANDOFF.md Session 52. This test
asserts the injected `commit_fn` fires after successful mutating requests and
stays silent everywhere else.
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

from app.models import init_db, get_db
from app.routes import create_app
from app.auth import create_session_token, SESSION_COOKIE

HX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture()
def commit_calls():
    return []


@pytest.fixture()
def client(commit_calls):
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = _TMP_DB
    models.DATABASE_PATH = _TMP_DB
    init_db()

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (company, job_title, url, pipeline_stage, date_found) "
            "VALUES (?, ?, ?, ?, date('now'))",
            ("Acme", "Director of RevOps", "https://example.com/job/1", "identified"),
        )
        job_id = cur.lastrowid

    app = create_app(commit_fn=lambda: commit_calls.append(1))
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    c.job_id = job_id
    return c


def test_commit_fires_after_successful_mutating_request(client, commit_calls):
    resp = client.post(f"/job/{client.job_id}/notes", data={"notes": "call recruiter"}, headers=HX_HEADERS)
    assert resp.status_code == 200
    assert commit_calls == [1]


def test_commit_does_not_fire_on_get(client, commit_calls):
    resp = client.get(f"/job/{client.job_id}")
    assert resp.status_code == 200
    assert commit_calls == []


def test_commit_does_not_fire_on_csrf_rejected_post(client, commit_calls):
    # No X-Requested-With header -> CSRFValidationMiddleware rejects before the
    # handler (and therefore any write) ever runs.
    resp = client.post(f"/job/{client.job_id}/notes", data={"notes": "should not land"})
    assert resp.status_code == 403
    assert commit_calls == []


def test_commit_does_not_fire_on_validation_error(client, commit_calls):
    # Declining without a reason is a 400 from advance_stage — no write happened.
    resp = client.post(
        f"/job/{client.job_id}/stage",
        data={"to_stage": "i_declined"},
        headers=HX_HEADERS,
    )
    assert resp.status_code == 400
    assert commit_calls == []


def test_commit_failure_does_not_break_the_response(commit_calls):
    def _boom():
        raise RuntimeError("volume unreachable")

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (company, job_title, url, pipeline_stage, date_found) "
            "VALUES (?, ?, ?, ?, date('now'))",
            ("Boomco", "Director of RevOps", "https://example.com/job/2", "identified"),
        )
        job_id = cur.lastrowid

    app = create_app(commit_fn=_boom)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())

    resp = c.post(f"/job/{job_id}/notes", data={"notes": "still saved"}, headers=HX_HEADERS)
    assert resp.status_code == 200

    with get_db() as conn:
        row = conn.execute("SELECT notes FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["notes"] == "still saved"


def test_no_commit_fn_is_a_safe_default():
    """Off-Modal hosts (app/asgi.py) call create_app() with no commit_fn — must not error."""
    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (company, job_title, url, pipeline_stage, date_found) "
            "VALUES (?, ?, ?, ?, date('now'))",
            ("Nocommitco", "Director of RevOps", "https://example.com/job/3", "identified"),
        )
        job_id = cur.lastrowid
    resp = c.post(f"/job/{job_id}/notes", data={"notes": "fine"}, headers=HX_HEADERS)
    assert resp.status_code == 200
