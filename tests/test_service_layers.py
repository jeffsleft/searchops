"""
Tests for service layer extraction: dashboard, job, and pipeline services.

Verifies that services return correctly structured dicts and handle edge cases.
"""
import os
import tempfile

# Environment must exist before any app import (app.auth/config read it at import time)
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest
from app.models import get_db, init_db
from app.services.dashboard_service import build_dashboard_data
from app.services.job_service import build_job_detail_data
from app.services.pipeline_service import build_pipeline_view_data
from app.routes import _enrich_job


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """Initialize test database once per test session."""
    init_db()


class TestBuildDashboardData:
    """Tests for dashboard_service.build_dashboard_data()"""

    def test_returns_dict_with_required_keys(self):
        """Verify the service returns all required keys."""
        data = build_dashboard_data("", _enrich_job)
        assert isinstance(data, dict)
        required_keys = {
            'live', 'high', 'recent', 'in_pipeline', 'interviewing',
            'avg_score', 'outreach_queue', 'total_scored', 'discovered_count',
            'followups_due', 'upcoming', 'last_scan_time', 'new_since_last',
        }
        assert required_keys.issubset(data.keys()), f"Missing keys: {required_keys - data.keys()}"

    def test_with_empty_archetype(self):
        """Verify service works with no archetype filter."""
        data = build_dashboard_data("", _enrich_job)
        assert isinstance(data['live'], list)
        assert isinstance(data['high'], list)
        assert data['avg_score'] >= 0

    def test_with_valid_archetype(self):
        """Verify service works with a valid archetype filter."""
        data = build_dashboard_data("GTM Ops", _enrich_job)
        assert isinstance(data['live'], list)
        # All jobs should be GTM Ops or have no archetype
        for job in data['live']:
            archetype = job.get('role_archetype')
            if archetype:
                assert archetype == "GTM Ops"

    def test_high_score_threshold(self):
        """Verify high-score list only contains jobs >= 7.0."""
        data = build_dashboard_data("", _enrich_job)
        for job in data['high']:
            assert job.get('final_score', 0) >= 7.0


class TestBuildJobDetailData:
    """Tests for job_service.build_job_detail_data()"""

    def test_returns_none_for_missing_job(self):
        """Verify service returns None when job doesn't exist."""
        result = build_job_detail_data(999999, _enrich_job)
        assert result is None

    def test_returns_dict_with_required_keys(self):
        """Verify the service returns all required keys when job exists."""
        with get_db() as conn:
            row = conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()
        if not row:
            pytest.skip("No jobs in database")

        job_id = row['id']
        data = build_job_detail_data(job_id, _enrich_job)
        assert data is not None
        required_keys = {
            'job', 'contacts', 'questions', 'questions_answered',
            'flags_fired', 'tech_stack',
        }
        assert required_keys.issubset(data.keys()), f"Missing keys: {required_keys - data.keys()}"

    def test_questions_answered_is_integer(self):
        """Verify questions_answered count is an integer."""
        with get_db() as conn:
            row = conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()
        if not row:
            pytest.skip("No jobs in database")

        data = build_job_detail_data(row['id'], _enrich_job)
        assert isinstance(data['questions_answered'], int)
        assert data['questions_answered'] >= 0

    def test_flags_fired_structure(self):
        """Verify flags_fired has correct structure."""
        with get_db() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE flags_json IS NOT NULL LIMIT 1").fetchone()
        if not row:
            pytest.skip("No jobs with flags in database")

        data = build_job_detail_data(row['id'], _enrich_job)
        for flag in data['flags_fired']:
            assert 'id' in flag
            assert 'label' in flag
            assert 'weight' in flag


class TestBuildPipelineViewData:
    """Tests for pipeline_service.build_pipeline_view_data()"""

    def test_returns_dict_with_required_keys(self):
        """Verify the service returns all required keys."""
        data = build_pipeline_view_data("", _enrich_job)
        assert isinstance(data, dict)
        required_keys = {
            'jobs_by_stage', 'stale_items', 'total', 'active', 'max_stage_count'
        }
        assert required_keys.issubset(data.keys()), f"Missing keys: {required_keys - data.keys()}"

    def test_jobs_by_stage_structure(self):
        """Verify jobs_by_stage is a dict of stage->jobs."""
        data = build_pipeline_view_data("", _enrich_job)
        assert isinstance(data['jobs_by_stage'], dict)
        for stage, jobs in data['jobs_by_stage'].items():
            assert isinstance(stage, str)
            assert isinstance(jobs, list)
            for job in jobs:
                assert isinstance(job, dict)
                assert 'id' in job

    def test_counts_are_non_negative(self):
        """Verify all count fields are non-negative integers."""
        data = build_pipeline_view_data("", _enrich_job)
        assert data['total'] >= 0
        assert data['active'] >= 0
        assert data['max_stage_count'] >= 1  # At least 1 for aggregation

    def test_with_archetype_filter(self):
        """Verify service respects archetype filter."""
        data = build_pipeline_view_data("CS Ops", _enrich_job)
        assert isinstance(data['jobs_by_stage'], dict)
        # All jobs should be CS Ops or have no archetype
        all_jobs = []
        for jobs in data['jobs_by_stage'].values():
            all_jobs.extend(jobs)
        for job in all_jobs:
            archetype = job.get('role_archetype')
            if archetype:
                assert archetype == "CS Ops"
