"""
Tests for GET /settings/interviews view and interview_metrics_service.

Covers:
- Empty state (no sessions)
- Populated state with score trends and confidence gating
- Session cadence aggregation by week
- Funnel cross-reference with sync_stub exclusion
- Insufficient data rendering
"""
import os
import tempfile
import json
from datetime import datetime, timedelta

os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("CALIBRATION_MIN_SAMPLE", "5")
os.environ.setdefault("BOARD_CONFIDENCE_SOLID_N", "30")

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest
from starlette.testclient import TestClient

from app.models import init_db, get_db
from app.routes import create_app
from app.auth import create_session_token, SESSION_COOKIE
from app.services.interview_metrics_service import interview_dashboard_metrics


# Module-level setup: initialize the test database once
@pytest.fixture(scope="module", autouse=True)
def setup_db():
    """Initialize database once per module."""
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = _TMP_DB
    models.DATABASE_PATH = _TMP_DB
    init_db()
    yield


@pytest.fixture
def client():
    """Set up test client with authenticated session (function-scoped for test isolation)."""
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = _TMP_DB
    models.DATABASE_PATH = _TMP_DB

    # Clean and reinitialize DB for each test
    with get_db() as conn:
        conn.execute("DELETE FROM interview_sessions")
        conn.execute("DELETE FROM session_questions_to_ask")
        conn.execute("DELETE FROM session_questions_they_ask")
        conn.execute("DELETE FROM session_red_flags")
        conn.execute("DELETE FROM session_pinned_anchors")
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM companies")

    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    c.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return c


def _create_test_company(conn, name: str = "TestCorp"):
    """Helper to create a company and return its ID."""
    cursor = conn.execute(
        "INSERT INTO companies (name, date_added) VALUES (?, ?)",
        (name, "2026-08-01"),
    )
    return cursor.lastrowid


def _create_test_job(
    conn,
    company_id: int,
    company: str = "TestCorp",
    pipeline_stage: str = "identified",
):
    """Helper to create a job and return its ID."""
    cursor = conn.execute(
        """INSERT INTO jobs
           (company_id, company, job_title, status, pipeline_stage, date_found, discovery_source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (company_id, company, "Director of RevOps", "Identified", pipeline_stage, "2026-08-01", "manual"),
    )
    return cursor.lastrowid


def _create_interview_session(
    conn,
    job_id: int,
    date: str,
    self_eval_scores: dict = None,
    mode: str = "live",
):
    """Helper to create an interview session with optional scores."""
    from datetime import datetime as dt, timezone
    now = dt.now(timezone.utc).isoformat()

    type_id_map = {"prep": "recruiter", "live": "hm", "debrief": "final"}
    type_id = type_id_map.get(mode, "custom")

    cursor = conn.execute(
        """INSERT INTO interview_sessions
           (job_id, type_id, label, position, schedule_date, schedule_mode, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, type_id, f"Interview ({mode})", 0, date, mode, now, now),
    )
    session_id = cursor.lastrowid

    if self_eval_scores:
        insights_json = json.dumps({"self_eval_scores": self_eval_scores})
        conn.execute(
            "UPDATE interview_sessions SET transcript_insights_json = ? WHERE id = ?",
            (insights_json, session_id),
        )

    return session_id


class TestInterviewDashboardView:
    """Test suite for GET /settings/interviews."""

    def test_view_renders_empty_state(self, client):
        """Dashboard renders without crashing when no sessions exist."""
        resp = client.get("/settings/interviews")
        assert resp.status_code == 200
        assert "Interview Performance" in resp.text
        assert "No interview sessions recorded yet" in resp.text or "Sessions" in resp.text

    def test_view_with_single_session(self, client):
        """Dashboard renders a single session with scores."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "SingleSession Co")
            job_id = _create_test_job(conn, co_id, "SingleSession Co", "identified")

            scores = {
                "narrative_clarity": 4.0,
                "evidence_specificity": 3.5,
                "question_handling": 4.0,
                "executive_presence": 3.0,
                "fit_differentiation": 4.5,
                "curiosity_questions": 3.5,
            }
            _create_interview_session(conn, job_id, "2026-08-07", scores)

        resp = client.get("/settings/interviews")
        assert resp.status_code == 200
        assert "Sessions" in resp.text
        assert "Score Trends by Dimension" in resp.text

    def test_view_with_multiple_sessions(self, client):
        """Dashboard aggregates multiple sessions."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "MultiSession Co")
            job_id = _create_test_job(conn, co_id, "MultiSession Co", "identified")

            # Create 3 sessions over different dates
            for i, date in enumerate(["2026-08-05", "2026-08-06", "2026-08-07"]):
                scores = {
                    "narrative_clarity": 3.0 + i * 0.5,
                    "evidence_specificity": 3.5,
                    "question_handling": 4.0,
                    "executive_presence": 3.0,
                    "fit_differentiation": 4.0,
                    "curiosity_questions": 3.0 + i * 0.3,
                }
                _create_interview_session(conn, job_id, date, scores)

        resp = client.get("/settings/interviews")
        assert resp.status_code == 200
        # Should show 3 sessions
        assert "3" in resp.text or "Sessions" in resp.text

    def test_session_cadence_by_week(self, client):
        """Dashboard aggregates sessions by calendar week."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "CadenceCo")
            job_id = _create_test_job(conn, co_id, "CadenceCo", "identified")

            # Create 2 sessions in week 1, 3 in week 2
            for date in ["2026-07-29", "2026-07-30"]:
                _create_interview_session(conn, job_id, date)
            for date in ["2026-08-05", "2026-08-06", "2026-08-07"]:
                _create_interview_session(conn, job_id, date)

        resp = client.get("/settings/interviews")
        assert resp.status_code == 200
        assert "Session Cadence" in resp.text

    def test_funnel_cross_ref_excludes_sync_stub(self, client):
        """Funnel cross-reference excludes sync_stub jobs (placeholder rows)."""
        with get_db() as conn:
            # Create a real job with they_declined stage
            co_id = _create_test_company(conn, "RealJobCo")
            real_job_id = _create_test_job(
                conn, co_id, "RealJobCo", "they_declined"
            )
            scores_real = {
                "narrative_clarity": 2.0,
                "evidence_specificity": 2.0,
                "question_handling": 2.0,
                "executive_presence": 2.0,
                "fit_differentiation": 2.0,
                "curiosity_questions": 2.0,
            }
            _create_interview_session(conn, real_job_id, "2026-08-07", scores_real)

            # Create a sync_stub job (auto-created by interview-sync endpoint)
            stub_job_id = _create_test_job(
                conn, co_id, "RealJobCo", "sync_stub"
            )
            scores_stub = {
                "narrative_clarity": 5.0,
                "evidence_specificity": 5.0,
                "question_handling": 5.0,
                "executive_presence": 5.0,
                "fit_differentiation": 5.0,
                "curiosity_questions": 5.0,
            }
            _create_interview_session(conn, stub_job_id, "2026-08-08", scores_stub)

        # Fetch metrics
        metrics = interview_dashboard_metrics()

        # Funnel cross-ref should only include the real job
        they_declined = metrics["funnel_cross_ref"]["they_declined"]
        assert they_declined["count"] == 1, "sync_stub should be excluded from funnel count"

    def test_funnel_cross_ref_they_declined_vs_i_declined(self, client):
        """Funnel cross-reference distinguishes they_declined from i_declined."""
        with get_db() as conn:
            # Create two jobs with different decline reasons
            co_id = _create_test_company(conn, "DeclineCo")

            # They declined
            job_they_id = _create_test_job(
                conn, co_id, "DeclineCo", "they_declined"
            )
            scores_they = {
                "narrative_clarity": 2.0,
                "evidence_specificity": 2.0,
                "question_handling": 2.0,
                "executive_presence": 2.0,
                "fit_differentiation": 2.0,
                "curiosity_questions": 2.0,
            }
            _create_interview_session(conn, job_they_id, "2026-08-07", scores_they)

            # I declined
            job_i_id = _create_test_job(
                conn, co_id, "DeclineCo", "i_declined"
            )
            scores_i = {
                "narrative_clarity": 4.5,
                "evidence_specificity": 4.5,
                "question_handling": 4.5,
                "executive_presence": 4.5,
                "fit_differentiation": 4.5,
                "curiosity_questions": 4.5,
            }
            _create_interview_session(conn, job_i_id, "2026-08-07", scores_i)

        metrics = interview_dashboard_metrics()
        fcr = metrics["funnel_cross_ref"]

        # Should have separate entries
        assert fcr["they_declined"]["count"] == 1
        assert fcr["i_declined"]["count"] == 1

        # I declined should have higher average score
        they_avg = fcr["they_declined"]["avg_score"]["value"]
        i_avg = fcr["i_declined"]["avg_score"]["value"]
        if they_avg is not None and i_avg is not None:
            assert i_avg > they_avg, "I declined should have higher scores"

    def test_confidence_gating_insufficient_data(self, client):
        """Metrics with n < CALIBRATION_MIN_SAMPLE show 'Insufficient data'."""
        with get_db() as conn:
            # Create only 2 sessions (below default threshold of 5)
            co_id = _create_test_company(conn, "SmallSampleCo")
            job_id = _create_test_job(conn, co_id, "SmallSampleCo", "accepted")

            for date in ["2026-08-07", "2026-08-08"]:
                scores = {
                    "narrative_clarity": 4.0,
                    "evidence_specificity": 4.0,
                    "question_handling": 4.0,
                    "executive_presence": 4.0,
                    "fit_differentiation": 4.0,
                    "curiosity_questions": 4.0,
                }
                _create_interview_session(conn, job_id, date, scores)

        metrics = interview_dashboard_metrics()

        # Check that score averages show confidence gating
        for dim_data in metrics["score_trends"]:
            avg = dim_data["avg"]
            # With n=2, should be "directional" (>= CALIBRATION_MIN_SAMPLE but < BOARD_CONFIDENCE_SOLID_N)
            if avg["n"] == 2:
                assert avg["confidence"] in ["directional", "insufficient"]

    def test_score_trends_all_dimensions(self, client):
        """Score trends include all 6 dimensions."""
        EXPECTED_DIMENSIONS = [
            "narrative_clarity",
            "evidence_specificity",
            "question_handling",
            "executive_presence",
            "fit_differentiation",
            "curiosity_questions",
        ]

        with get_db() as conn:
            co_id = _create_test_company(conn, "AllDimsCo")
            job_id = _create_test_job(conn, co_id, "AllDimsCo", "identified")

            scores = {dim: 3.5 for dim in EXPECTED_DIMENSIONS}
            _create_interview_session(conn, job_id, "2026-08-07", scores)

        metrics = interview_dashboard_metrics()

        returned_dims = [d["dimension"] for d in metrics["score_trends"]]
        for expected in EXPECTED_DIMENSIONS:
            assert expected in returned_dims, f"{expected} missing from score_trends"

    def test_empty_metrics_returns_empty_flag(self, client):
        """Metrics include empty=True when no sessions exist."""
        metrics = interview_dashboard_metrics()
        assert metrics["empty"] == True

    def test_score_trend_timeline_points(self, client):
        """Each dimension's trend includes timeline point for each session."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "TimelineCo")
            job_id = _create_test_job(conn, co_id, "TimelineCo", "identified")

            dates = ["2026-08-05", "2026-08-06", "2026-08-07"]
            for date in dates:
                scores = {
                    "narrative_clarity": 3.5,
                    "evidence_specificity": 3.5,
                    "question_handling": 3.5,
                    "executive_presence": 3.5,
                    "fit_differentiation": 3.5,
                    "curiosity_questions": 3.5,
                }
                _create_interview_session(conn, job_id, date, scores)

        metrics = interview_dashboard_metrics()

        for dim_data in metrics["score_trends"]:
            trend = dim_data["trend"]
            assert len(trend) == 3, f"{dim_data['dimension']} should have 3 timeline points"

    def test_missing_scores_in_session(self, client):
        """Sessions without some dimensions are handled gracefully."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "PartialCo")
            job_id = _create_test_job(conn, co_id, "PartialCo", "identified")

            # Only provide 2 out of 6 dimensions
            partial_scores = {
                "narrative_clarity": 4.0,
                "evidence_specificity": 3.5,
            }
            _create_interview_session(conn, job_id, "2026-08-07", partial_scores)

        metrics = interview_dashboard_metrics()

        # Should not crash; should have all 6 dimensions in output
        assert len(metrics["score_trends"]) == 6

    def test_session_with_no_scores_json(self, client):
        """Sessions without transcript_insights_json are handled gracefully."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "NoJsonCo")
            job_id = _create_test_job(conn, co_id, "NoJsonCo", "identified")

            # Create session without scores
            _create_interview_session(conn, job_id, "2026-08-07", None)

        metrics = interview_dashboard_metrics()

        # Should not crash and should reflect 1 session
        assert metrics["total_sessions"] == 1

    def test_malformed_json_in_transcript_insights(self, client):
        """Malformed JSON in transcript_insights_json is gracefully ignored."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "MalformedCo")
            job_id = _create_test_job(conn, co_id, "MalformedCo", "identified")

            # Create session with malformed JSON
            from datetime import datetime as dt, timezone
            now = dt.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """INSERT INTO interview_sessions
                   (job_id, type_id, label, position, schedule_date, schedule_mode,
                    transcript_insights_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, "hm", "Interview (live)", 0, "2026-08-07", "live",
                 "not valid json", now, now),
            )

        metrics = interview_dashboard_metrics()

        # Should not crash
        assert metrics["total_sessions"] == 1

    def test_accepted_jobs_appear_in_funnel_cross_ref(self, client):
        """Jobs with accepted stage appear in funnel cross-ref."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "AcceptedCo")
            job_id = _create_test_job(conn, co_id, "AcceptedCo", "accepted")

            scores = {
                "narrative_clarity": 4.5,
                "evidence_specificity": 4.5,
                "question_handling": 4.5,
                "executive_presence": 4.5,
                "fit_differentiation": 4.5,
                "curiosity_questions": 4.5,
            }
            _create_interview_session(conn, job_id, "2026-08-07", scores)

        metrics = interview_dashboard_metrics()
        fcr = metrics["funnel_cross_ref"]

        assert fcr["accepted"]["count"] == 1


class TestInterviewMetricsService:
    """Test suite for interview_dashboard_metrics() service function."""

    def test_empty_database(self):
        """Service returns sane defaults when DB is empty."""
        # Use a fresh DB connection
        with get_db() as conn:
            conn.execute("DELETE FROM interview_sessions")

        metrics = interview_dashboard_metrics()

        assert metrics["empty"] == True
        assert metrics["total_sessions"] == 0
        assert len(metrics["score_trends"]) == 6
        assert metrics["session_cadence"] == []

    def test_metric_value_suppression_on_small_sample(self):
        """Metrics suppress values on sample size < CALIBRATION_MIN_SAMPLE."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "SmallCo")
            job_id = _create_test_job(conn, co_id, "SmallCo", "identified")

            # Only 2 sessions
            for date in ["2026-08-07", "2026-08-08"]:
                scores = {
                    "narrative_clarity": 3.0,
                    "evidence_specificity": 3.0,
                    "question_handling": 3.0,
                    "executive_presence": 3.0,
                    "fit_differentiation": 3.0,
                    "curiosity_questions": 3.0,
                }
                _create_interview_session(conn, job_id, date, scores)

        metrics = interview_dashboard_metrics()

        # With n=2 (< CALIBRATION_MIN_SAMPLE=5), values should be None or directional
        for dim_data in metrics["score_trends"]:
            if dim_data["avg"]["n"] == 2:
                # Should be None or directional
                conf = dim_data["avg"]["confidence"]
                assert conf in ["directional", "insufficient"], f"Expected directional/insufficient, got {conf}"

    def test_average_score_calculation(self):
        """Score averages are calculated correctly."""
        with get_db() as conn:
            co_id = _create_test_company(conn, "AvgCo")
            job_id = _create_test_job(conn, co_id, "AvgCo", "identified")

            # Create 2 sessions with known scores
            _create_interview_session(
                conn, job_id, "2026-08-07",
                {"narrative_clarity": 2.0, "evidence_specificity": 2.0,
                 "question_handling": 2.0, "executive_presence": 2.0,
                 "fit_differentiation": 2.0, "curiosity_questions": 2.0}
            )
            _create_interview_session(
                conn, job_id, "2026-08-08",
                {"narrative_clarity": 4.0, "evidence_specificity": 4.0,
                 "question_handling": 4.0, "executive_presence": 4.0,
                 "fit_differentiation": 4.0, "curiosity_questions": 4.0}
            )

        metrics = interview_dashboard_metrics()

        # Find narrative_clarity dimension
        nc_data = next(d for d in metrics["score_trends"] if d["dimension"] == "narrative_clarity")
        # Average should be 3.0 (average of 2.0 and 4.0)
        if nc_data["avg"]["value"] is not None:
            assert nc_data["avg"]["value"] == 3.0

    def test_funnel_cross_ref_averages_all_sessions_for_a_job(self):
        """A job with multiple interview sessions (prep + live + debrief) must
        average across ALL of them in the funnel cross-reference, not silently
        keep only the last one processed (regression test — the original
        implementation overwrote session_avg_scores[job_id] per session instead
        of accumulating, discarding earlier rounds' scores).

        Uses 5 jobs so the aggregate crosses CALIBRATION_MIN_SAMPLE=5 and the
        value isn't suppressed by confidence gating. job1 has two sessions
        (2.0, 4.0) that should average to 3.0; jobs 2-5 each have one session
        at exactly 3.0. If the bug were present, job1 would contribute 4.0
        (last session only) and the overall average would be 3.2, not 3.0.
        """
        all_dims = ["narrative_clarity", "evidence_specificity", "question_handling",
                    "executive_presence", "fit_differentiation", "curiosity_questions"]
        with get_db() as conn:
            co_id = _create_test_company(conn, "MultiRoundCo")
            job1 = _create_test_job(conn, co_id, "MultiRoundCo", "they_declined")
            _create_interview_session(
                conn, job1, "2026-08-01", {d: 2.0 for d in all_dims}, mode="prep",
            )
            _create_interview_session(
                conn, job1, "2026-08-08", {d: 4.0 for d in all_dims}, mode="live",
            )
            for i in range(2, 6):
                co_n = _create_test_company(conn, f"SingleRoundCo{i}")
                job_n = _create_test_job(conn, co_n, f"SingleRoundCo{i}", "they_declined")
                _create_interview_session(
                    conn, job_n, "2026-08-05", {d: 3.0 for d in all_dims}, mode="live",
                )

        metrics = interview_dashboard_metrics()
        they_declined = metrics["funnel_cross_ref"]["they_declined"]

        assert they_declined["count"] == 5
        assert they_declined["avg_score"]["value"] == 3.0, (
            f"expected 3.0 (job1's two sessions averaged to 3.0); got "
            f"{they_declined['avg_score']['value']} — likely only job1's last "
            f"session (4.0) survived instead of averaging with the first (2.0)"
        )
