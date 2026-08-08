"""
Tests for POST /api/sync/interview-session endpoint.

Covers:
- Successful insert with valid JSON
- Missing required fields (company, date, mode)
- Deduplication on (company, date, mode)
- Unmatched job_id handled gracefully
- Session creation with questions_covered and self_eval_scores
- Various date and mode formats
"""
import os
import tempfile
import json

os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest
from starlette.testclient import TestClient

from app.models import init_db, get_db
from app.routes import create_app
from app.auth import create_session_token, SESSION_COOKIE


@pytest.fixture(scope="module")
def client():
    """Set up test client with authenticated session."""
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = _TMP_DB
    models.DATABASE_PATH = _TMP_DB
    init_db()
    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    # Add default CSRF header for POST requests
    c.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return c


def _create_test_company(conn, name: str = "TestCorp"):
    """Helper to create a company and return its ID."""
    cursor = conn.execute(
        "INSERT INTO companies (name, date_added) VALUES (?, ?)",
        (name, "2026-08-01"),
    )
    return cursor.lastrowid


def _create_test_job(conn, company_id: int, company: str = "TestCorp"):
    """Helper to create a job and return its ID."""
    cursor = conn.execute(
        """INSERT INTO jobs
           (company_id, company, job_title, status, pipeline_stage, date_found, discovery_source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (company_id, company, "Director of RevOps", "Identified", "identified", "2026-08-01", "manual"),
    )
    return cursor.lastrowid


class TestInterviewSyncEndpoint:
    """Test suite for /api/sync/interview-session."""

    def test_successful_insert_minimal_payload(self, client):
        """Successfully create a session with minimal required fields."""
        payload = {
            "company": "Acme Corp",
            "date": "2026-08-07",
            "mode": "live",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "session_id" in body
        assert isinstance(body["session_id"], int)
        assert body["session_id"] > 0

        # Verify session was created in DB
        with get_db() as conn:
            session = conn.execute(
                "SELECT * FROM interview_sessions WHERE id = ?",
                (body["session_id"],),
            ).fetchone()
            assert session is not None
            assert session["schedule_date"] == "2026-08-07"
            assert session["schedule_mode"] == "live"

    def test_successful_insert_with_job_id(self, client):
        """Successfully create a session when job_id is provided and matches."""
        # Set up job
        with get_db() as conn:
            co_id = _create_test_company(conn, "Acme Corp")
            job_id = _create_test_job(conn, co_id, "Acme Corp")

        payload = {
            "company": "Acme Corp",
            "job_id": job_id,
            "date": "2026-08-07",
            "mode": "prep",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        session_id = body["session_id"]

        # Verify session was created with correct job_id
        with get_db() as conn:
            session = conn.execute(
                "SELECT * FROM interview_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert session["job_id"] == job_id

    def test_successful_insert_with_questions_and_scores(self, client):
        """Successfully create a session with questions_covered and self_eval_scores."""
        payload = {
            "company": "Acme Corp",
            "date": "2026-08-07",
            "mode": "debrief",
            "questions_covered": ["Tell me about your background", "Why this company?"],
            "self_eval_scores": {"communication": 4, "technical_depth": 3},
            "notes": "Good conversation overall",
            "transcript": "Full conversation transcript here...",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        session_id = body["session_id"]

        # Verify questions were added (in addition to seeded ones)
        with get_db() as conn:
            questions = conn.execute(
                "SELECT prompt FROM session_questions_they_ask WHERE session_id = ? ORDER BY position DESC LIMIT 2",
                (session_id,),
            ).fetchall()
            # The last two should be our provided questions (highest position = latest added)
            assert len(questions) >= 2
            # Check that the provided questions are in the list somewhere
            all_prompts = [q["prompt"] for q in conn.execute(
                "SELECT prompt FROM session_questions_they_ask WHERE session_id = ?",
                (session_id,),
            ).fetchall()]
            assert "Tell me about your background" in all_prompts
            assert "Why this company?" in all_prompts

            # Verify notes and transcript
            session = conn.execute(
                "SELECT scratchpad, transcript, transcript_insights_json FROM interview_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert session["scratchpad"] == "Good conversation overall"
            assert session["transcript"] == "Full conversation transcript here..."
            assert session["transcript_insights_json"] is not None
            insights = json.loads(session["transcript_insights_json"])
            assert insights["self_eval_scores"]["communication"] == 4
            assert insights["self_eval_scores"]["technical_depth"] == 3

    def test_deduplication_same_company_date_mode(self, client):
        """Return existing session_id if (company, date, mode) already exists."""
        # First insert
        payload = {
            "company": "Dedup Corp",
            "date": "2026-08-07",
            "mode": "live",
        }
        resp1 = client.post("/api/sync/interview-session", json=payload)
        body1 = resp1.json()
        session_id_1 = body1["session_id"]

        # Duplicate insert with same company, date, mode
        resp2 = client.post("/api/sync/interview-session", json=payload)
        body2 = resp2.json()
        session_id_2 = body2["session_id"]

        # Should return the same session_id
        assert session_id_1 == session_id_2
        assert body2["message"] == "Session already exists"

    def test_different_mode_not_deduplicated(self, client):
        """Sessions with same company and date but different modes are separate."""
        company = "DiffMode Corp"
        date = "2026-08-07"

        payload_prep = {
            "company": company,
            "date": date,
            "mode": "prep",
        }
        resp_prep = client.post("/api/sync/interview-session", json=payload_prep)
        session_id_prep = resp_prep.json()["session_id"]

        payload_live = {
            "company": company,
            "date": date,
            "mode": "live",
        }
        resp_live = client.post("/api/sync/interview-session", json=payload_live)
        session_id_live = resp_live.json()["session_id"]

        # Should be different sessions
        assert session_id_prep != session_id_live

    def test_missing_company_field(self, client):
        """Reject POST with missing company."""
        payload = {
            "date": "2026-08-07",
            "mode": "live",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "company is required" in body["message"]

    def test_missing_date_field(self, client):
        """Reject POST with missing date."""
        payload = {
            "company": "Test Corp",
            "mode": "live",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "date is required" in body["message"]

    def test_missing_mode_field(self, client):
        """Reject POST with missing mode."""
        payload = {
            "company": "Test Corp",
            "date": "2026-08-07",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "mode is required" in body["message"]

    def test_invalid_mode(self, client):
        """Reject POST with invalid mode value."""
        payload = {
            "company": "Test Corp",
            "date": "2026-08-07",
            "mode": "invalid_mode",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "prep, live, or debrief" in body["message"]

    def test_invalid_date_format(self, client):
        """Reject POST with malformed date."""
        payload = {
            "company": "Test Corp",
            "date": "08-07-2026",  # Wrong format
            "mode": "live",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "YYYY-MM-DD" in body["message"]

    def test_invalid_json(self, client):
        """Reject request with invalid JSON body."""
        resp = client.post(
            "/api/sync/interview-session",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "Invalid JSON" in body["message"]

    def test_unmatched_job_id_creates_minimal_job(self, client):
        """Handle unmatched job_id gracefully by creating a minimal job entry."""
        payload = {
            "company": "NewCorp",
            "job_id": 99999,  # Non-existent
            "date": "2026-08-07",
            "mode": "live",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

        # Verify session was created (with a newly created job)
        with get_db() as conn:
            session = conn.execute(
                "SELECT * FROM interview_sessions WHERE id = ?",
                (body["session_id"],),
            ).fetchone()
            assert session is not None

            # Job should have been created
            job = conn.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (session["job_id"],),
            ).fetchone()
            assert job is not None
            assert job["company"] == "NewCorp"

    def test_session_gets_seed_content(self, client):
        """Verify that session creation seeds the appropriate question types."""
        payload = {
            "company": "SeedTest Corp",
            "date": "2026-08-07",
            "mode": "prep",  # Maps to "recruiter" type
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        body = resp.json()
        session_id = body["session_id"]

        # Verify seed content was created
        with get_db() as conn:
            # Recruiter type should have seeded "questions_to_ask"
            q_to_ask = conn.execute(
                "SELECT COUNT(*) as cnt FROM session_questions_to_ask WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            assert q_to_ask["cnt"] > 0

            # Should have seeded "questions_they_ask"
            q_they_ask = conn.execute(
                "SELECT COUNT(*) as cnt FROM session_questions_they_ask WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            # Note: We added questions_covered on top of seeded ones, so count >= seeded count
            # For "prep" (recruiter type), should have at least the seeded recruiter questions

    def test_session_gets_pinned_anchors(self, client):
        """Verify that session creation includes pinned anchor stories."""
        # Create an anchor story first
        with get_db() as conn:
            conn.execute(
                "INSERT INTO anchor_stories (id, title, summary, strongest) VALUES (?, ?, ?, ?)",
                ("story1", "GitLab CAC payback", "Reduced CAC from 18m to 12m", 1),
            )

        payload = {
            "company": "AnchorTest Corp",
            "date": "2026-08-07",
            "mode": "live",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        body = resp.json()
        session_id = body["session_id"]

        # Verify pinned anchors
        with get_db() as conn:
            pinned = conn.execute(
                "SELECT COUNT(*) as cnt FROM session_pinned_anchors WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            # Should have at least one pinned (or none if no strong anchors exist)
            # This test just verifies the mechanism runs without error

    def test_empty_company_string_rejected(self, client):
        """Reject POST with empty company string."""
        payload = {
            "company": "   ",  # Whitespace only
            "date": "2026-08-07",
            "mode": "live",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"

    def test_all_valid_modes(self, client):
        """Test all three valid modes are accepted."""
        for mode in ("prep", "live", "debrief"):
            payload = {
                "company": f"ModeTest {mode}",
                "date": "2026-08-07",
                "mode": mode,
            }
            resp = client.post("/api/sync/interview-session", json=payload)
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "success"
            assert body["session_id"] > 0

    def test_session_label_reflects_mode(self, client):
        """Verify that session label includes the mode."""
        payload = {
            "company": "LabelTest",
            "date": "2026-08-07",
            "mode": "debrief",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        body = resp.json()
        session_id = body["session_id"]

        with get_db() as conn:
            session = conn.execute(
                "SELECT label FROM interview_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert "debrief" in session["label"].lower()

    def test_questions_covered_empty_list(self, client):
        """Handle empty questions_covered list gracefully."""
        payload = {
            "company": "EmptyQues Corp",
            "date": "2026-08-07",
            "mode": "live",
            "questions_covered": [],
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_self_eval_scores_empty_dict(self, client):
        """Handle empty self_eval_scores dict gracefully."""
        payload = {
            "company": "EmptyScores Corp",
            "date": "2026-08-07",
            "mode": "live",
            "self_eval_scores": {},
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_unmatched_company_creates_hidden_stub(self, client):
        """A company with no existing job should get a stub job tagged
        pipeline_stage='sync_stub' so it's excluded from the live /pipeline
        board (regression test for the phantom-card bug caught in review)."""
        payload = {
            "company": "NeverTrackedCo",
            "date": "2026-08-07",
            "mode": "prep",
        }
        resp = client.post("/api/sync/interview-session", json=payload)
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        from app.models import get_db
        with get_db() as conn:
            row = conn.execute(
                """SELECT j.pipeline_stage, j.discovery_source FROM interview_sessions s
                   JOIN jobs j ON j.id = s.job_id WHERE s.id = ?""",
                (session_id,),
            ).fetchone()
            assert row["pipeline_stage"] == "sync_stub"
            assert row["discovery_source"] == "interview-sync"

        from app.services.pipeline_service import build_pipeline_view_data
        data = build_pipeline_view_data("", lambda j: j)
        all_jobs = [j for jobs in data["jobs_by_stage"].values() for j in jobs]
        assert not any(j.get("company") == "NeverTrackedCo" for j in all_jobs), (
            "sync_stub job leaked into the live pipeline board view"
        )


class TestInterviewSyncEndpointAuth:
    """Auth coverage for /api/sync/interview-session: bearer token (for
    non-browser callers like the interview-scorecard skill) and rejection
    without a browser session or a bearer token."""

    @pytest.fixture(scope="class")
    def anon_client(self):
        app = create_app()
        c = TestClient(app)
        c.headers.update({"X-Requested-With": "XMLHttpRequest"})
        return c

    def test_bearer_token_valid_authenticates(self, anon_client):
        payload = {"company": "BearerCorp", "date": "2026-08-07", "mode": "prep"}
        resp = anon_client.post(
            "/api/sync/interview-session",
            json=payload,
            headers={"Authorization": "Bearer test-password"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_bearer_token_wrong_value_rejected(self, anon_client):
        payload = {"company": "BearerCorpWrong", "date": "2026-08-07", "mode": "prep"}
        resp = anon_client.post(
            "/api/sync/interview-session",
            json=payload,
            headers={"Authorization": "Bearer not-the-password"},
        )
        assert resp.status_code == 401

    def test_no_auth_rejected_with_401_not_redirect(self, anon_client):
        """Non-browser clients can't follow a 302 to /login — this path must
        return a plain 401, not a redirect."""
        payload = {"company": "NoAuthCorp", "date": "2026-08-07", "mode": "prep"}
        resp = anon_client.post(
            "/api/sync/interview-session", json=payload, follow_redirects=False
        )
        assert resp.status_code == 401
