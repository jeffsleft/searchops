"""
Regression test for the promote-from-discovery scoring path.

Before the 2026-07-08 fix, job_promote_from_discovery scored inline and wrote
only 3 numeric columns (final_score, deterministic_score, llm_adjustment),
silently dropping the evidence table, mismatches, tailored bullets, and hooks
that every other scoring path persists. The route now goes through the
canonical service path (score_job_from_text_and_persist), so the full record
must land in the DB.
"""
import os
import tempfile

# --- environment must exist before any app import ---------------------------
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest
from starlette.testclient import TestClient

from app.models import init_db
from app.routes import create_app
from app.auth import create_session_token, SESSION_COOKIE


FAKE_SCORE_RECORD = {
    "company": "Forgeline",
    "job_title": "Director of RevOps",
    "final_score": 6.0,
    "deterministic_score": 5.5,
    "llm_adjustment": 0.5,
    "auto_rejected": False,
    "reject_reason": None,
    "pros": "Strong greenfield mandate",
    "cons": "Early-stage ambiguity",
    "greenfield": "Yes",
    "sector": "DevTools",
    "salary_range_detected": "$180k-$220k",
    "match_score": 2.5,
    "match_summary": "Solid partial match.",
    "evidence": [
        {"jd_requirement": "Build RevOps from scratch",
         "matched_accomplishment": "Built CS Ops infrastructure at scale",
         "strength": "Strong"},
    ],
    "mismatches": [
        {"jd_requirement": "Marketo administration", "gap": "No corpus evidence",
         "severity": "Low"},
    ],
    "tailored_bullets": [
        {"company": "GitLab", "bullet": "Renewal Operations — example bullet"},
    ],
    "cover_letter_hooks": ["Example hook sentence."],
    "differentiator_themes": ["finance+ops bridge"],
    "adjustment_weights_score": 1.5,
    "tech_stack_detected": {"crm": "Salesforce"},
    "flags": [],
    "role_archetype": "RevOps",
}


@pytest.fixture(scope="module")
def client():
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = _TMP_DB
    models.DATABASE_PATH = _TMP_DB
    init_db()
    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


def test_promote_persists_full_score_record(client, monkeypatch):
    """Promoting a discovered job persists the FULL score record, not 3 columns."""
    from app.models import get_db

    jd_text = ("We need a Director of Revenue Operations to build our GTM systems "
               "from scratch. You will own CRM architecture, forecasting, and the "
               "renewal motion end to end.")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO jobs (company, job_title, jd_text, pipeline_stage, status,
                                 date_found, discovery_source)
               VALUES (?, ?, ?, 'discovered', 'Discovered', '2026-07-08', 'scan')""",
            ("Forgeline", "Director of RevOps", jd_text),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # The route imports these names at module level from scoring_service; the
    # service function itself calls score_job via its own module namespace.
    monkeypatch.setattr(
        "app.services.scoring_service.score_job", lambda text: dict(FAKE_SCORE_RECORD)
    )

    resp = client.post(
        f"/job/{job_id}/promote",
        headers={"X-Requested-With": "XMLHttpRequest"},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text

    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    row = dict(row)

    assert row["pipeline_stage"] == "identified"
    assert row["final_score"] == 6.0
    # The regression: these were silently dropped by the old 3-column UPDATE.
    assert row["match_evidence_json"] and "Build RevOps from scratch" in row["match_evidence_json"]
    assert row["match_mismatches_json"] and "Marketo" in row["match_mismatches_json"]
    assert row["match_bullets_json"] and "GitLab" in row["match_bullets_json"]
    assert row["match_hooks_json"] and "Example hook" in row["match_hooks_json"]
    assert row["match_summary"] == "Solid partial match."
    assert row["sector"] == "DevTools"
