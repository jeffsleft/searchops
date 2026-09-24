"""
Regression test for the silent jd_insufficient pass-through (found 2026-09-16).

score_job() returns {"jd_insufficient": True} (no final_score key) when the JD
content is too thin to score. score_job_from_text_and_persist() used to pass that
dict straight to persist_score_record_to_job(), which does a bare
score_record.get("final_score") with no default — writing NULL to jobs.final_score
while still reporting status="success". Several jobs (Sardine, two EliseAI roles,
Built Technologies) ended up silently "scored" with no score at all this way during
a stub-triage session; nothing downstream ever flagged it as a failure.

Also covers the adjacent MatchResult/Evidence schema hardening: Evidence.strength
and Mismatch fields used to be required with no default, so the LLM omitting one
field on one evidence row failed Pydantic validation for the whole match result.
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

import app.config as config
import app.models as models
from app.models import get_db, init_db
from app.jobs.persist import save_job_to_db
from app.scoring.schemas import Evidence, Mismatch, MatchResult


@pytest.fixture()
def temp_db(monkeypatch):
    tmp_path = os.path.join(tempfile.mkdtemp(), "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path)
    monkeypatch.setattr(models, "DATABASE_PATH", tmp_path)
    init_db()
    yield tmp_path


def test_jd_insufficient_does_not_silently_report_success(temp_db, monkeypatch):
    from app.services import scoring_service

    job_id = save_job_to_db(
        "https://boards.greenhouse.io/acme/jobs/9",
        {
            "company": "Acme", "job_title": "Director RevOps", "final_score": None,
            "evidence": [], "mismatches": [], "tailored_bullets": [],
            "cover_letter_hooks": [], "differentiator_themes": [],
            "tech_stack_detected": {}, "flags": [], "role_archetype": "RevOps",
        },
        jd_text="",
    )

    monkeypatch.setattr(scoring_service, "score_job", lambda jd: {"jd_insufficient": True})

    result = scoring_service.score_job_from_text_and_persist(job_id, "x" * 150)

    assert result["status"] == "error"
    assert "insufficient" in result["error"].lower() or "thin" in result["error"].lower()

    with get_db() as conn:
        row = conn.execute("SELECT final_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["final_score"] is None, "jd_insufficient must never overwrite final_score with a fresh NULL write"


def test_jd_insufficient_short_circuits_before_persist_call(temp_db, monkeypatch):
    """Belt-and-suspenders: persist_score_record_to_job must not even be called
    for a jd_insufficient record, regardless of what it would do with it."""
    from app.services import scoring_service

    monkeypatch.setattr(scoring_service, "score_job", lambda jd: {"jd_insufficient": True})
    called = {"count": 0}
    monkeypatch.setattr(
        scoring_service, "persist_score_record_to_job",
        lambda *a, **k: called.__setitem__("count", called["count"] + 1),
    )

    result = scoring_service.score_job_from_text_and_persist(1, "x" * 150)

    assert result["status"] == "error"
    assert called["count"] == 0


def test_evidence_accepts_missing_strength():
    """The LLM occasionally omits `strength` on one evidence row — that must not
    fail validation for the whole row (or, via MatchResult, the whole response)."""
    ev = Evidence(jd_requirement="5+ years RevOps", matched_accomplishment="Built RevOps at GitLab")
    assert ev.strength == "Moderate"


def test_mismatch_accepts_missing_fields():
    mm = Mismatch()
    assert mm.severity == "Medium"


def test_match_result_with_partial_evidence_row_does_not_raise():
    result = MatchResult(
        match_score=2.0,
        evidence=[{"jd_requirement": "req", "matched_accomplishment": "acc"}],
    )
    assert result.evidence[0].strength == "Moderate"
