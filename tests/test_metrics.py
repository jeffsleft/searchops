"""Tests for the progress-instrumentation layer: usage capture, error capture,
structured-logging config, anti-fabrication metric gating, and integrity checks.

Env must be set before importing app modules (app.config / app.auth read it at
import time), so the os.environ block sits at the very top.
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
from starlette.routing import Route
from starlette.testclient import TestClient

import app.config as config
import app.models as models

config.DATABASE_PATH = _TMP_DB
models.DATABASE_PATH = _TMP_DB

from app.models import init_db, get_db
from app.routes import create_app
from app.auth import create_session_token, SESSION_COOKIE


@pytest.fixture(scope="module")
def app_client():
    init_db()
    app = create_app()
    # Synthetic always-failing route to exercise the app-level exception handler.
    async def _boom(request):
        raise RuntimeError("synthetic-failure")
    app.router.routes.append(Route("/_boomtest", _boom, methods=["GET"]))
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


def test_init_db_idempotent():
    # Running twice must not raise (KR: fresh init_db creates new tables cleanly).
    init_db()
    init_db()
    with get_db() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"usage_events", "error_events"} <= names


def test_usage_event_recorded_per_request(app_client):
    with get_db() as conn:
        conn.execute("DELETE FROM usage_events")
    app_client.get("/settings/calibration")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT method, route_template, status, duration_ms, is_hx FROM usage_events "
            "WHERE route_template = '/settings/calibration'"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["method"] == "GET" and r["status"] == 200
    assert r["duration_ms"] is not None


def test_hx_request_flagged(app_client):
    with get_db() as conn:
        conn.execute("DELETE FROM usage_events")
    app_client.get("/settings/calibration", headers={"HX-Request": "true"})
    with get_db() as conn:
        r = conn.execute(
            "SELECT is_hx FROM usage_events WHERE route_template='/settings/calibration' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert r["is_hx"] == 1


def test_unhandled_exception_records_one_error_event(app_client):
    with get_db() as conn:
        conn.execute("DELETE FROM error_events")
        conn.execute("DELETE FROM usage_events")
    resp = app_client.get("/_boomtest")
    assert resp.status_code == 500
    with get_db() as conn:
        errs = conn.execute(
            "SELECT route_template, exc_type, request_id FROM error_events"
        ).fetchall()
        usage = conn.execute(
            "SELECT status FROM usage_events WHERE route_template='/_boomtest'"
        ).fetchone()
    assert len(errs) == 1
    assert errs[0]["exc_type"] == "RuntimeError"
    assert errs[0]["request_id"]  # correlation id captured
    assert usage is not None and usage["status"] == 500  # usage row also recorded


def test_configure_logging_idempotent_single_handler():
    import logging
    from app.observability import configure_logging
    configure_logging()
    configure_logging()
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, "_searchops_handler", False)]
    assert len(ours) == 1  # reconfigure must not stack handlers


# --- Anti-fabrication: thin data suppresses the number; ample data reveals it ---

def _reset_jobs(conn):
    """Clear jobs and every child table that FK-references it (FK-safe order).

    The suite shares one DATABASE_PATH across test files, so other tests may have
    left score_history / pipeline_history rows; delete children before jobs.
    """
    conn.execute("DELETE FROM application_outcomes")
    conn.execute("DELETE FROM score_history")
    conn.execute("DELETE FROM pipeline_history")
    conn.execute("DELETE FROM jobs")


def _seed_scored_jobs_with_interviews(n_jobs, n_interviews):
    with get_db() as conn:
        _reset_jobs(conn)
        for i in range(n_jobs):
            conn.execute(
                "INSERT INTO jobs (company, date_found, final_score, auto_rejected, pipeline_stage, match_score) "
                "VALUES (?,?,?,?,?,?)",
                (f"Co{i}", "2026-06-01", 8.5, 0, "hm_interview", 1.5),
            )
            jid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO application_outcomes (job_id, outcome) VALUES (?, 'applied')", (jid,))
            if i < n_interviews:
                conn.execute("INSERT INTO application_outcomes (job_id, outcome) VALUES (?, 'interview')", (jid,))


def test_engine_quality_insufficient_on_thin_data():
    _seed_scored_jobs_with_interviews(3, 2)  # n=3 < CALIBRATION_MIN_SAMPLE(5)
    from app.services.metrics_service import engine_quality
    top = engine_quality()["score_cohort"][0]
    assert top["confidence"] == "insufficient"
    assert top["interview_rate"] is None  # no scalar on thin data
    # No Brier score anywhere (v2 redline) — the key must not exist.
    assert "mean_brier" not in engine_quality()


def test_engine_quality_solid_on_ample_data():
    _seed_scored_jobs_with_interviews(40, 30)  # n=40 >= BOARD_CONFIDENCE_SOLID_N(30)
    from app.services.metrics_service import engine_quality
    top = engine_quality()["score_cohort"][0]
    assert top["confidence"] == "solid"
    assert top["interview_rate"] == 75.0  # 30/40


def test_integrity_flags_history_gap():
    # A job advanced past the initial stages with no pipeline_history row is a
    # capture gap — the check that validates the single-writer refactor.
    with get_db() as conn:
        _reset_jobs(conn)
        conn.execute(
            "INSERT INTO jobs (company, date_found, pipeline_stage) VALUES ('Gap Co', '2026-06-01', 'hm_interview')"
        )
    from app.services.metrics_service import integrity_checks
    checks = {c["check"]: c["count"] for c in integrity_checks()["checks"]}
    assert checks["Advanced jobs with no stage history"] >= 1


def test_prune_observability_runs_clean():
    from app.models import prune_observability_tables
    prune_observability_tables(180)  # must not raise
