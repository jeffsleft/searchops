"""
Tests for the posting-age/staleness flag (W3-C, docs/plan-posting-age-flag.md).

Covers the two pieces that were entirely untested despite being live in prod:
ScoringResult's posting_age_days coercion/clamping, and persistence of both
fields to the jobs row.
"""
import pytest

from app.models import get_db
from app.scoring.schemas import ScoringResult
from app.services.scoring_service import persist_score_record_to_job


class TestScoringResultPostingAge:
    def test_valid_int_passes_through(self):
        result = ScoringResult(posting_age_days=8, posting_date_raw="Posted 8 Days Ago")
        assert result.posting_age_days == 8
        assert result.posting_date_raw == "Posted 8 Days Ago"

    def test_missing_defaults_to_none(self):
        result = ScoringResult()
        assert result.posting_age_days is None
        assert result.posting_date_raw is None

    def test_non_numeric_string_coerces_to_none(self):
        result = ScoringResult(posting_age_days="unknown")
        assert result.posting_age_days is None

    def test_numeric_string_coerces_to_int(self):
        result = ScoringResult(posting_age_days="14")
        assert result.posting_age_days == 14

    def test_negative_clamps_to_zero(self):
        result = ScoringResult(posting_age_days=-5)
        assert result.posting_age_days == 0


class TestPersistPostingAge:
    @pytest.fixture
    def job_id(self):
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (company, job_title, url, pipeline_stage, date_found) "
                "VALUES (?, ?, ?, ?, date('now'))",
                ("Acme", "Director of RevOps", "https://example.com/job/posting-age", "identified"),
            )
            return cur.lastrowid

    def test_persists_posting_age_and_date(self, job_id):
        score_record = {
            "company": "Acme",
            "job_title": "Director of RevOps",
            "final_score": 7.5,
            "posting_age_days": 15,
            "posting_date_raw": "2 weeks ago",
        }
        persist_score_record_to_job(job_id, score_record)

        with get_db() as conn:
            row = conn.execute(
                "SELECT posting_age_days, posting_date_raw FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row["posting_age_days"] == 15
        assert row["posting_date_raw"] == "2 weeks ago"

    def test_null_posting_age_persists_as_null(self, job_id):
        score_record = {"company": "Acme", "job_title": "Director of RevOps", "final_score": 7.5}
        persist_score_record_to_job(job_id, score_record)

        with get_db() as conn:
            row = conn.execute(
                "SELECT posting_age_days, posting_date_raw FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row["posting_age_days"] is None
        assert row["posting_date_raw"] is None
