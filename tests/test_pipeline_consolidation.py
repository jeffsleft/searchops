"""
CRITICAL GATE TEST: verify all pipeline_stage changes route through record_stage_change() helper.

This test ensures the spec's "single write path" requirement is met by:
1. Grepping the codebase for raw UPDATE statements on pipeline_stage (outside the helper)
2. Testing that record_stage_change() correctly writes pipeline_history with from_stage
3. Testing terminal transitions trigger calibration_service.record_outcome()
4. Testing get_calibration_summary() returns insufficient_data flag when n < threshold
5. Testing small calibration buckets (n < 3) return null rates
6. Testing snapshot idempotency (same date = one row, not duplicated)
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

import app.config as config
import app.models as models
from app.models import get_db, init_db
from app.services.pipeline_service import record_stage_change


@pytest.fixture()
def temp_db(monkeypatch):
    """Point DATABASE_PATH at a fresh temp file and run the real init_db() (schema +
    migrations), so tests exercise the actual production schema rather than a
    hand-rolled subset."""
    tmp_path = os.path.join(tempfile.mkdtemp(), "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path)
    monkeypatch.setattr(models, "DATABASE_PATH", tmp_path)
    init_db()
    yield tmp_path


def _insert_job(conn, **overrides):
    defaults = {
        "company": "TestCorp",
        "job_title": "Engineer",
        "pipeline_stage": "identified",
        "auto_rejected": 0,
    }
    defaults.update(overrides)
    cols = list(defaults.keys())
    placeholders = ", ".join(["?"] * len(cols))
    cursor = conn.execute(
        f"INSERT INTO jobs (date_found, {', '.join(cols)}) VALUES (date('now'), {placeholders})",
        [defaults[c] for c in cols],
    )
    return cursor.lastrowid


def test_no_stray_pipeline_stage_writers():
    """
    CRITICAL: grep the codebase and assert NO raw UPDATE jobs SET pipeline_stage
    statements exist outside pipeline_service.record_stage_change().

    This is the enforcement gate that proves all three callers were refactored.
    Matches by file identity, not a hardcoded line number, so it doesn't go stale
    (or silently stop enforcing anything) the next time pipeline_service.py is edited.
    """
    repo_root = Path(__file__).parent.parent
    app_dir = repo_root / "app"

    result = subprocess.run(
        ["grep", "-rn", "--exclude-dir=__pycache__", "UPDATE jobs SET pipeline_stage", str(app_dir)],
        capture_output=True,
        text=True,
    )

    lines = [line for line in result.stdout.strip().split("\n") if line]

    stray = [line for line in lines if "app/services/pipeline_service.py" not in line]
    assert not stray, f"Found raw UPDATE jobs SET pipeline_stage outside the helper:\n{chr(10).join(stray)}"

    in_helper = [line for line in lines if "app/services/pipeline_service.py" in line]
    assert len(in_helper) == 1, (
        f"Expected exactly 1 UPDATE jobs SET pipeline_stage statement (inside record_stage_change), "
        f"found {len(in_helper)}:\n{chr(10).join(in_helper)}"
    )


def test_record_stage_change_writes_history_with_from_stage(temp_db):
    """record_stage_change() writes exactly one pipeline_history row with from_stage."""
    with get_db() as conn:
        job_id = _insert_job(conn, pipeline_stage="identified")
        record_stage_change(conn, job_id, "evaluated", note="test transition", changed_by="test")

    with get_db() as conn:
        history = conn.execute(
            "SELECT from_stage, to_stage, notes FROM pipeline_history WHERE job_id = ?",
            (job_id,),
        ).fetchall()

    assert len(history) == 1, f"Expected 1 history row, got {len(history)}"
    assert history[0]["from_stage"] == "identified"
    assert history[0]["to_stage"] == "evaluated"
    assert history[0]["notes"] == "test transition"


def test_record_stage_change_terminal_triggers_outcome(temp_db):
    """Terminal transitions must write a matching application_outcomes row.

    This exercises record_stage_change -> record_outcome sharing the same `conn`.
    Passing a *second* get_db() connection here would deadlock: SQLite only allows
    one writer transaction per file, and the outer `with get_db()` block below is
    still open (uncommitted) at the point record_outcome() would try to write.
    """
    with get_db() as conn:
        job_id = _insert_job(conn, pipeline_stage="recruiter")
        record_stage_change(conn, job_id, "applied", note="applied today", changed_by="test")

    with get_db() as conn:
        outcomes = conn.execute(
            "SELECT outcome, notes FROM application_outcomes WHERE job_id = ?",
            (job_id,),
        ).fetchall()

    assert len(outcomes) == 1, f"Expected 1 application_outcomes row, got {len(outcomes)}"
    assert outcomes[0]["outcome"] == "applied"


def test_get_calibration_summary_insufficient_data_flag(temp_db, monkeypatch):
    """insufficient_data must be true when n_with_outcome < CALIBRATION_MIN_SAMPLE."""
    monkeypatch.setattr(config, "CALIBRATION_MIN_SAMPLE", 5)
    import app.services.calibration_service as calibration_service
    monkeypatch.setattr(calibration_service, "CALIBRATION_MIN_SAMPLE", 5)

    with get_db() as conn:
        for i in range(2):  # 2 < 5 -> insufficient
            job_id = _insert_job(conn, final_score=6.0)
            conn.execute(
                "INSERT INTO application_outcomes (job_id, outcome) VALUES (?, ?)",
                (job_id, "applied"),
            )

    summary = calibration_service.get_calibration_summary()
    assert summary["n_with_outcome"] == 2
    assert summary["insufficient_data"] is True
    assert "mean_brier" not in summary


def test_calibration_small_bucket_null_rate(temp_db, monkeypatch):
    """Buckets with n < 3 report rate=None regardless of the global CALIBRATION_MIN_SAMPLE gate."""
    import app.services.calibration_service as calibration_service

    with get_db() as conn:
        # 1 "Low" job -> bucket n=1 (< 3, must be null)
        _insert_job(
            conn, final_score=3.0, auto_rejected=0, jd_text="jd text",
            pipeline_stage="identified", interview_probability="Low",
        )
        # 3 "High" jobs -> bucket n=3 (>= 3, must be a real rate)
        for _ in range(3):
            _insert_job(
                conn, final_score=8.0, auto_rejected=0, jd_text="jd text",
                pipeline_stage="identified", interview_probability="High",
            )

    summary = calibration_service.get_calibration_summary()
    buckets = {b["label"]: b for b in summary["hit_rate_by_bucket"]}

    assert buckets["Low"]["n"] == 1
    assert buckets["Low"]["rate"] is None

    assert buckets["High"]["n"] == 3
    assert buckets["High"]["rate"] is not None


def test_progress_snapshots_idempotency(temp_db, monkeypatch):
    """Running the snapshot job twice on the same date yields one row, not two."""
    # Never touch the real docs/wins.md from a test run.
    import app.crons.progress as progress_cron
    monkeypatch.setattr(progress_cron, "WINS_AUTOEMIT_ENABLED", False)

    result1 = progress_cron.snapshot_progress()
    result2 = progress_cron.snapshot_progress()

    assert result1["snapshot_date"] == result2["snapshot_date"]

    with get_db() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM progress_snapshots WHERE snapshot_date = ?",
            (result1["snapshot_date"],),
        ).fetchone()

    assert rows["n"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
