"""Every write tells the user whether it saved (base.html save layer).

Write routes that fail but answer 200, so htmx still swaps the inline error in,
must carry X-Save-Status: failed; otherwise the page shows "Saved" over an
error. Successful writes must not carry it.
"""
import pytest
from starlette.testclient import TestClient

from app.auth import SESSION_COOKIE, create_session_token
from app.models import get_db
from app.routes import create_app

HX = {"X-Requested-With": "XMLHttpRequest", "HX-Request": "true"}


@pytest.fixture()
def client():
    c = TestClient(create_app())
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


@pytest.fixture()
def job_id():
    with get_db() as conn:
        return conn.execute(
            "INSERT INTO jobs (company, job_title, pipeline_stage, date_found) "
            "VALUES ('Acme', 'Dir RevOps', 'discovered', date('now'))").lastrowid


@pytest.fixture()
def company_id():
    with get_db() as conn:
        return conn.execute(
            "INSERT INTO companies (name, hunt_enabled, date_added) VALUES ('Acme Co', 1, date('now'))"
        ).lastrowid


def test_empty_note_is_marked_failed(client, job_id):
    r = client.post(f"/job/{job_id}/notes", data={"notes": "  "}, headers=HX)
    assert r.status_code == 200
    assert r.headers.get("X-Save-Status") == "failed"


def test_saved_note_is_not_marked_failed(client, job_id):
    r = client.post(f"/job/{job_id}/notes", data={"notes": "Called the recruiter"}, headers=HX)
    assert r.status_code == 200
    assert "X-Save-Status" not in r.headers


def test_paste_and_score_with_no_text_is_marked_failed(client, job_id):
    r = client.post(f"/job/{job_id}/paste-and-score", data={"jd_text": ""}, headers=HX)
    assert r.headers.get("X-Save-Status") == "failed"


def test_monitoring_toggle_returns_the_new_label(client, company_id):
    r = client.post(f"/targets/{company_id}/toggle", headers=HX)
    assert r.status_code == 200
    assert "Enable Monitoring" in r.text  # was on, now paused
    with get_db() as conn:
        assert conn.execute("SELECT hunt_enabled FROM companies WHERE id=?", (company_id,)).fetchone()[0] == 0
    r = client.post(f"/targets/{company_id}/toggle", headers=HX)
    assert "Pause" in r.text


def test_monitoring_toggle_on_missing_company_is_an_error(client):
    r = client.post("/targets/999999/toggle", headers=HX)
    assert r.status_code == 404


@pytest.mark.parametrize("field", ["hook", "interviewers", "scratchpad", "transcript"])
def test_prep_autosave_to_a_missing_session_is_an_error(client, field):
    r = client.patch(f"/prep/sessions/999999/{field}", data={"value": "typed text"}, headers=HX)
    assert r.status_code == 404


def test_prep_autosave_saves_to_a_real_session(client, job_id):
    with get_db() as conn:
        sid = conn.execute(
            "INSERT INTO interview_sessions (job_id, type_id, label, created_at, updated_at) "
            "VALUES (?, 'recruiter', 'Screen', datetime('now'), datetime('now'))", (job_id,)
        ).lastrowid
    r = client.patch(f"/prep/sessions/{sid}/scratchpad", data={"value": "ask about CS headcount"}, headers=HX)
    assert r.status_code == 200
    with get_db() as conn:
        assert conn.execute("SELECT scratchpad FROM interview_sessions WHERE id=?", (sid,)).fetchone()[0] == "ask about CS headcount"
    r = client.patch(f"/prep/sessions/{sid}/schedule", data={"date": "2026-10-01", "mode": "video"}, headers=HX)
    assert r.status_code == 200


def test_failed_dismiss_carries_its_reason_in_a_header(client, job_id, monkeypatch):
    from urllib.parse import unquote
    monkeypatch.setattr("app.pipeline.tracker.advance_stage",
                        lambda *a, **k: {"ok": False, "error": "Stage change refused — reason"})
    r = client.post(f"/job/{job_id}/dismiss", headers=HX)
    assert r.headers.get("X-Save-Status") == "failed"
    assert unquote(r.headers["X-Save-Message"]) == "Stage change refused — reason"


def test_renaming_a_missing_session_is_an_error(client):
    r = client.patch("/prep/sessions/999999", data={"label": "Panel"}, headers=HX)
    assert r.status_code == 404
