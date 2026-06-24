"""
Unit tests for app/services/job_actions.py — the business logic extracted out of
routes.py during the service-layer refactor. Covers the no-LLM paths: stage
updates (pure DB) and the guard branches of the scoring helpers.
"""
import os
import tempfile

# Env must exist before any app import (app.config / app.auth read at import time).
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest

import app.config as config
import app.models as models

config.DATABASE_PATH = _TMP_DB
models.DATABASE_PATH = _TMP_DB

from app.models import get_db, init_db
from app.services.job_actions import (
    fetch_and_score_stub, update_job_stage, score_new_job_from_input,
)


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


def _insert_job(**cols) -> int:
    cols.setdefault("date_found", "2026-06-13")
    fields = ", ".join(cols.keys())
    placeholders = ", ".join("?" for _ in cols)
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO jobs ({fields}) VALUES ({placeholders})", tuple(cols.values())
        )
        return cur.lastrowid


def test_score_new_job_missing_input():
    assert score_new_job_from_input("", "") == {"status": "missing_input"}


def test_fetch_and_score_stub_not_found():
    assert fetch_and_score_stub(999999) == {"status": "not_found"}


def test_fetch_and_score_stub_no_url():
    job_id = _insert_job(company="Acme", job_title="RevOps Lead", pipeline_stage="identified")
    assert fetch_and_score_stub(job_id) == {"status": "no_url"}


def test_fetch_and_score_stub_linkedin_blocked():
    job_id = _insert_job(
        company="Acme", job_title="RevOps Lead", pipeline_stage="identified",
        url="https://www.linkedin.com/jobs/view/1234567890",
    )
    assert fetch_and_score_stub(job_id) == {"status": "linkedin_blocked"}


def test_update_job_stage_invalid():
    job_id = _insert_job(company="Acme", job_title="RevOps", pipeline_stage="discovered")
    assert update_job_stage(job_id, "not_a_real_stage") == {"status": "invalid_stage"}


def test_update_job_stage_persists_and_flags_promotion():
    job_id = _insert_job(company="Acme", job_title="RevOps", pipeline_stage="discovered")
    result = update_job_stage(job_id, "outreach")

    assert result["status"] == "ok"
    assert result["promoted"] is True
    assert result["job"]["pipeline_stage"] == "outreach"

    # History row written
    with get_db() as conn:
        hist = conn.execute(
            "SELECT to_stage FROM pipeline_history WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    assert hist["to_stage"] == "outreach"
