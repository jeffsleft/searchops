"""Regression tests: every scoring event must leave a score_history row.

Background (2026-08-17). The engine has two score-persistence paths:

  * `save_job_to_db()`            (app/jobs/persist.py)  — URL-keyed
  * `persist_score_record_to_job()` (app/services/scoring_service.py) — job_id-keyed

Only the first one appended to `score_history`. The second silently overwrote
`jobs.final_score` in place with no record of what produced it, so any job scored
through the job_id path (rescore, promote-from-discovery, discovery auto-score)
lost its history. Observed in production: 26 of 117 scored jobs had no history row
at all, and one job's live score disagreed with its own newest history entry with
nothing to explain the change.

These tests pin both paths so the asymmetry cannot come back.
"""
from datetime import date

import pytest

from app.models import get_db, init_db


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with get_db() as conn:
        conn.execute("DELETE FROM score_history")
        conn.execute("DELETE FROM jobs")


def _score_record(**overrides):
    record = {
        "company": "Forgeline",
        "job_title": "Director, GTM Operations",
        "final_score": 8.4,
        "deterministic_score": 6.0,
        "llm_adjustment": 1.0,
        "match_score": 3.4,
        "adjustment_weights_score": 1.5,
        "auto_rejected": False,
        "evidence": [],
        "mismatches": [],
    }
    record.update(overrides)
    return record


def _make_job(company="Forgeline", url="https://example.com/job/1"):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO jobs (company, job_title, url, date_found, pipeline_stage)
               VALUES (?,?,?,?,?)""",
            (company, "Director, GTM Operations", url, date.today().isoformat(), "discovered"),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _history(job_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM score_history WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()


class TestJobIdPath:
    """persist_score_record_to_job — the path that was missing history."""

    def test_scoring_appends_a_history_row(self):
        from app.services.scoring_service import persist_score_record_to_job

        job_id = _make_job()
        persist_score_record_to_job(job_id, _score_record())

        rows = _history(job_id)
        assert len(rows) == 1, "a scoring event must leave exactly one history row"

    def test_history_row_matches_the_persisted_score(self):
        from app.services.scoring_service import persist_score_record_to_job

        job_id = _make_job()
        persist_score_record_to_job(job_id, _score_record())

        row = _history(job_id)[0]
        with get_db() as conn:
            job = conn.execute(
                "SELECT final_score, match_score FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()

        # The live score and its newest history row must never disagree. A job
        # whose stored score had no matching history row is what made the
        # production discrepancy unexplainable.
        assert row["final_score"] == job["final_score"] == 8.4
        assert row["match_score"] == job["match_score"] == 3.4
        assert row["deterministic_score"] == 6.0
        assert row["llm_adjustment"] == 1.0
        assert row["adjustment_weights_score"] == 1.5

    def test_rescoring_appends_rather_than_replaces(self):
        """Two scoring events, two rows — that is what makes it a history."""
        from app.services.scoring_service import persist_score_record_to_job

        job_id = _make_job()
        persist_score_record_to_job(job_id, _score_record(final_score=8.4))
        persist_score_record_to_job(job_id, _score_record(final_score=6.1, match_score=1.2))

        rows = _history(job_id)
        assert [r["final_score"] for r in rows] == [8.4, 6.1]

    def test_auto_rejected_job_still_records_history(self):
        """An auto-reject is a real engine verdict and belongs in the record."""
        from app.services.scoring_service import persist_score_record_to_job

        job_id = _make_job()
        persist_score_record_to_job(
            job_id, _score_record(final_score=0.0, auto_rejected=True,
                                  reject_reason="blocked sector")
        )
        assert len(_history(job_id)) == 1

    def test_scored_at_is_populated(self):
        """Without a timestamp the table cannot answer 'which engine scored this'."""
        from app.services.scoring_service import persist_score_record_to_job

        job_id = _make_job()
        persist_score_record_to_job(job_id, _score_record())
        assert _history(job_id)[0]["scored_at"] is not None

    def test_unknown_job_id_writes_nothing(self):
        """No job row means no update and no orphaned history row."""
        from app.services.scoring_service import persist_score_record_to_job

        persist_score_record_to_job(999999, _score_record())
        assert _history(999999) == []


class TestUrlPath:
    """save_job_to_db already recorded history. Pin it so it stays that way."""

    def test_new_job_records_history(self):
        from app.jobs.persist import save_job_to_db

        job_id = save_job_to_db("https://example.com/job/new", _score_record())
        assert len(_history(job_id)) == 1

    def test_both_paths_agree_on_the_columns_they_write(self):
        """The two writers drifting apart is the class of bug being fixed."""
        from app.jobs.persist import save_job_to_db
        from app.services.scoring_service import persist_score_record_to_job

        url_job = save_job_to_db("https://example.com/job/a", _score_record())
        id_job = _make_job(url="https://example.com/job/b")
        persist_score_record_to_job(id_job, _score_record())

        recorded = lambda r: {k: r[k] for k in (  # noqa: E731
            "final_score", "deterministic_score", "llm_adjustment",
            "match_score", "adjustment_weights_score")}
        assert recorded(_history(url_job)[0]) == recorded(_history(id_job)[0])


def test_no_scored_job_lacks_history():
    """Integrity invariant, the one that failed in production.

    Any job carrying a final_score must have at least one history row.
    """
    from app.services.scoring_service import persist_score_record_to_job

    job_id = _make_job()
    persist_score_record_to_job(job_id, _score_record())

    with get_db() as conn:
        orphans = conn.execute(
            """SELECT j.id FROM jobs j
               WHERE j.final_score IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM score_history h WHERE h.job_id = j.id)"""
        ).fetchall()
    assert orphans == [], f"jobs with a score but no history: {[o['id'] for o in orphans]}"
