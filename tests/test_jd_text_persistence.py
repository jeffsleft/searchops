"""
Regression tests for the jd_text persistence gap (2026-07-15 prod check).

save_job_to_db() (app/jobs/persist.py) never had a jd_text column in either its
INSERT or UPDATE — every job scored through score_new_job_from_input's URL path
(the dashboard "+ Score a Job" form, stub-add, stub-retry) landed with a full
score but an empty jd_text, making it un-rescorable later. Root cause was
structural: all three callers had jd_text in scope and passed it to score_job(),
but never forwarded it into save_job_to_db().

Also covers the adjacent MatchResult schema bug found while diagnosing this:
schemas.py bounded match_score to [-3, 3] while match.py/prompts.py/the profile
all use ±4.0 (same bug class Session 47 already fixed for L3, never applied
to L2) — a legitimate match_score of 3.8 failed Pydantic validation on every
call, silently masked by match.py's manual fallback.
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
from app.scoring.schemas import MatchResult

_JD = "Full job description text for the role, long enough to matter." * 5


@pytest.fixture()
def temp_db(monkeypatch):
    tmp_path = os.path.join(tempfile.mkdtemp(), "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path)
    monkeypatch.setattr(models, "DATABASE_PATH", tmp_path)
    init_db()
    yield tmp_path


def _minimal_score_record(**overrides) -> dict:
    rec = {
        "company": "Acme", "job_title": "Director RevOps", "final_score": 7.5,
        "deterministic_score": 5.0, "llm_adjustment": 0.5, "auto_rejected": False,
        "reject_reason": None, "pros": "p", "cons": "c", "greenfield": "Yes",
        "sector": "DevTools", "salary_range_detected": "$200k", "match_score": 2.0,
        "match_summary": "match", "evidence": [], "mismatches": [],
        "tailored_bullets": [], "cover_letter_hooks": [], "differentiator_themes": [],
        "adjustment_weights_score": 1.0, "tech_stack_detected": {}, "flags": [],
        "role_archetype": "RevOps",
    }
    rec.update(overrides)
    return rec


def test_save_job_to_db_persists_jd_text_on_insert(temp_db):
    job_id = save_job_to_db(
        "https://boards.greenhouse.io/acme/jobs/1", _minimal_score_record(), jd_text=_JD
    )
    with get_db() as conn:
        row = conn.execute("SELECT jd_text FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["jd_text"] == _JD


def test_save_job_to_db_caps_jd_text_at_30000_chars(temp_db):
    huge = "x" * 40000
    job_id = save_job_to_db(
        "https://boards.greenhouse.io/acme/jobs/2", _minimal_score_record(), jd_text=huge
    )
    with get_db() as conn:
        row = conn.execute("SELECT jd_text FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert len(row["jd_text"]) == 30000


def test_save_job_to_db_update_path_persists_jd_text(temp_db):
    url = "https://boards.greenhouse.io/acme/jobs/3"
    job_id = save_job_to_db(url, _minimal_score_record(final_score=5.0), jd_text="")
    with get_db() as conn:
        row = conn.execute("SELECT jd_text FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["jd_text"] == ""

    save_job_to_db(url, _minimal_score_record(final_score=6.0), jd_text=_JD)
    with get_db() as conn:
        row = conn.execute("SELECT jd_text, final_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["jd_text"] == _JD
    assert row["final_score"] == 6.0


def test_save_job_to_db_update_does_not_clobber_existing_jd_text(temp_db):
    """A rescore call that doesn't refetch the JD (jd_text='') must not blank out
    a jd_text a prior call already stored."""
    url = "https://boards.greenhouse.io/acme/jobs/4"
    job_id = save_job_to_db(url, _minimal_score_record(final_score=5.0), jd_text=_JD)

    save_job_to_db(url, _minimal_score_record(final_score=7.0), jd_text="")
    with get_db() as conn:
        row = conn.execute("SELECT jd_text, final_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["jd_text"] == _JD, "existing jd_text must survive an update with no new jd_text"
    assert row["final_score"] == 7.0


def test_score_new_job_from_input_persists_jd_text(temp_db, monkeypatch):
    """The dashboard '+ Score a Job' path (job_actions.score_new_job_from_input) —
    the exact path that produced the Zoom job (id 176) with a full score and an
    empty jd_text — must now store the fetched JD."""
    from app.services.job_actions import score_new_job_from_input

    monkeypatch.setattr("app.services.job_actions._fetch_jd_text", lambda url: _JD)
    monkeypatch.setattr("app.services.job_actions.score_job", lambda jd: _minimal_score_record())

    result = score_new_job_from_input("https://boards.greenhouse.io/zoom/jobs/1", "")
    assert result["status"] == "scored"
    job_id = result["score_record"]["_job_id"]

    with get_db() as conn:
        row = conn.execute("SELECT jd_text FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["jd_text"] == _JD


def test_match_result_accepts_positive_3_8():
    """3.8 is a legitimate L2 match_score under the documented ±4.0 range —
    must not raise a Pydantic validation error."""
    result = MatchResult(match_score=3.8, match_summary="strong fit")
    assert result.match_score == 3.8


def test_match_result_accepts_negative_3_8():
    result = MatchResult(match_score=-3.8, match_summary="poor fit")
    assert result.match_score == -3.8


def test_match_result_still_rejects_out_of_range():
    with pytest.raises(Exception):
        MatchResult(match_score=4.5, match_summary="x")
