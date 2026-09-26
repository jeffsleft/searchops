"""
W1-A watchdog rewire: newly discovered roles are auto-scored through the canonical
scoring path (fetch JD → L1 free → L2/L3 LLM → ≥8 Slack alert), with the expensive LLM
layers capped per scan run.

These tests exercise app.discovery.hunter._auto_score_discovery in isolation:
  - a fresh discovery reaches a full score and (if ≥8) fires the Slack alert,
  - the per-run LLM budget caps full scoring,
  - auto-rejects are persisted but do NOT consume the budget.
The JD fetch, the L1 auto-reject decision, the LLM scoring, and the Slack post are all
mocked — the point under test is the watchdog control flow, not the engine internals.
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

_JD = (
    "We are hiring a Director of Revenue Operations to build our GTM systems from "
    "scratch. You will own CRM architecture, forecasting, renewals, and the full "
    "RevOps stack end to end for a fast-growing DevTools company." * 4
)


def _fake_record(final_score: float) -> dict:
    return {
        "company": "Acme", "job_title": "Director RevOps",
        "final_score": final_score, "deterministic_score": 5.0, "llm_adjustment": 0.5,
        "auto_rejected": False, "reject_reason": None, "pros": "p", "cons": "c",
        "greenfield": "Yes", "sector": "DevTools", "salary_range_detected": "$200k",
        "match_score": 2.0, "match_summary": "match",
        "evidence": [{"jd_requirement": "Build RevOps", "matched_accomplishment": "did it", "strength": "Strong"}],
        "mismatches": [], "tailored_bullets": [], "cover_letter_hooks": [],
        "differentiator_themes": [], "adjustment_weights_score": 1.0,
        "tech_stack_detected": {}, "flags": [], "role_archetype": "RevOps",
    }


@pytest.fixture()
def temp_db(monkeypatch):
    tmp_path = os.path.join(tempfile.mkdtemp(), "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path)
    monkeypatch.setattr(models, "DATABASE_PATH", tmp_path)
    init_db()
    yield tmp_path


def _insert_discovered(conn, url="https://boards.greenhouse.io/acme/jobs/1") -> int:
    cur = conn.execute(
        "INSERT INTO jobs (company, job_title, url, pipeline_stage, date_found) "
        "VALUES ('Acme', 'Director RevOps', ?, 'discovered', date('now'))",
        (url,),
    )
    return cur.lastrowid


def _wire(monkeypatch, *, final_score=9.5, rejected=False):
    """Mock the JD fetch, L1 decision, LLM scoring, and Slack post. Returns a dict that
    records how many times the ≥8 alert fired."""
    calls = {"alerts": 0}
    monkeypatch.setattr("app.jobs.fetch._fetch_jd_text", lambda url: _JD)
    monkeypatch.setattr("app.scoring.engine.check_auto_reject",
                        lambda jd, profile: (rejected, "blocked sector" if rejected else None))
    monkeypatch.setattr("app.services.scoring_service.score_job",
                        lambda jd: _fake_record(final_score))

    def _alert(job_id, rec):
        calls["alerts"] += 1
        return True
    monkeypatch.setattr("app.notifications.slack.send_high_score_alert", _alert)
    return calls


def test_fresh_discovery_gets_scored_and_alerts(temp_db, monkeypatch):
    """A freshly discovered role reaches a full 4-layer score with evidence rows and,
    at ≥8, fires the Slack alert — zero manual steps. Budget is consumed."""
    from app.discovery.hunter import _auto_score_discovery

    with get_db() as conn:
        job_id = _insert_discovered(conn)

    calls = _wire(monkeypatch, final_score=9.5, rejected=False)
    budget = {"remaining": 10}
    status = _auto_score_discovery(job_id, "https://boards.greenhouse.io/acme/jobs/1", {}, budget)

    assert status == "success"
    assert budget["remaining"] == 9, "a full-scored survivor consumes one budget slot"
    assert calls["alerts"] == 1, "score ≥8 must fire the Slack high-score alert"

    with get_db() as conn:
        row = conn.execute("SELECT final_score, match_evidence_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["final_score"] == 9.5
    assert row["match_evidence_json"] and "Build RevOps" in row["match_evidence_json"]


def test_below_alert_threshold_scores_without_alert(temp_db, monkeypatch):
    """A survivor scoring <8 is still fully scored, but no Slack alert fires."""
    from app.discovery.hunter import _auto_score_discovery

    with get_db() as conn:
        job_id = _insert_discovered(conn)
    calls = _wire(monkeypatch, final_score=6.0, rejected=False)
    budget = {"remaining": 10}
    status = _auto_score_discovery(job_id, "https://boards.greenhouse.io/acme/jobs/1", {}, budget)

    assert status == "success"
    assert budget["remaining"] == 9
    assert calls["alerts"] == 0

    with get_db() as conn:
        row = conn.execute("SELECT final_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["final_score"] == 6.0


def test_over_cap_survivor_is_deferred(temp_db, monkeypatch):
    """With the LLM budget spent, an L1 survivor is deferred (not scored) rather than
    burning tokens past the cap."""
    from app.discovery.hunter import _auto_score_discovery

    with get_db() as conn:
        job_id = _insert_discovered(conn)
    calls = _wire(monkeypatch, final_score=9.5, rejected=False)
    budget = {"remaining": 0}
    status = _auto_score_discovery(job_id, "https://boards.greenhouse.io/acme/jobs/1", {}, budget)

    assert status == "deferred_over_cap"
    assert calls["alerts"] == 0
    with get_db() as conn:
        row = conn.execute("SELECT final_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["final_score"] is None, "deferred job must stay unscored"


def test_auto_reject_does_not_consume_budget(temp_db, monkeypatch):
    """An L1 auto-reject is persisted through the canonical path but never consumes the
    LLM cap — the cap guards the expensive L2/L3 layers, which auto-rejects skip."""
    from app.discovery.hunter import _auto_score_discovery

    with get_db() as conn:
        job_id = _insert_discovered(conn)
    _wire(monkeypatch, final_score=0.0, rejected=True)
    # score_job's real auto-reject branch makes a metadata call + compute_final_score;
    # here score_job is mocked, so persistence still runs but the budget must be intact.
    budget = {"remaining": 3}
    _auto_score_discovery(job_id, "https://boards.greenhouse.io/acme/jobs/1", {}, budget)

    assert budget["remaining"] == 3, "auto-rejects must not consume the LLM budget"


def test_run_discovery_scan_logs_started_and_completed(temp_db, monkeypatch):
    """The 06:00 UTC watchdog tick left no task_log trace even when it ran cleanly
    (2026-07-15 prod check) — run_discovery_scan() must always log a 'started' and
    a 'completed'/'partial' entry with found/scored counts, so silence is
    diagnosable without pulling Modal container logs."""
    from app.discovery import hunter
    from app.models import get_db

    monkeypatch.setattr(hunter, "load_hunt_config", lambda: {})

    hunter.run_discovery_scan()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT task_type, status, message FROM task_log WHERE task_type = 'discovery_scan' ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "started"
    assert rows[1]["status"] == "completed"
    assert "Scanned 0" in rows[1]["message"]


def test_linkedin_url_skipped(temp_db, monkeypatch):
    """LinkedIn blocks automated fetching — skip without spending anything."""
    from app.discovery.hunter import _auto_score_discovery

    with get_db() as conn:
        job_id = _insert_discovered(conn, url="https://www.linkedin.com/jobs/view/123")
    calls = _wire(monkeypatch, final_score=9.5, rejected=False)
    budget = {"remaining": 5}
    status = _auto_score_discovery(job_id, "https://www.linkedin.com/jobs/view/123", {}, budget)

    assert status == "skip_unfetchable"
    assert budget["remaining"] == 5
    assert calls["alerts"] == 0


def test_feed_description_is_scored_without_refetching_the_page(temp_db, monkeypatch):
    """The job board's API already returned the JD. Score from it: re-fetching the
    posting page fails on JS-rendered boards and left roles unscored (2026-09-24)."""
    from app.discovery.hunter import _auto_score_discovery

    with get_db() as conn:
        job_id = _insert_discovered(conn)
    _wire(monkeypatch, final_score=6.0)
    fetched = []
    monkeypatch.setattr("app.jobs.fetch._fetch_jd_text", lambda url: fetched.append(url) or "")
    status = _auto_score_discovery(job_id, "https://boards.greenhouse.io/acme/jobs/1", {},
                                   {"remaining": 10}, feed_text=_JD)
    assert status == "success"
    assert fetched == [], "a long enough feed description must not trigger a page re-fetch"


def test_thin_feed_description_falls_back_to_fetching(temp_db, monkeypatch):
    from app.discovery.hunter import _auto_score_discovery

    with get_db() as conn:
        job_id = _insert_discovered(conn)
    _wire(monkeypatch, final_score=6.0)
    status = _auto_score_discovery(job_id, "https://boards.greenhouse.io/acme/jobs/1", {},
                                   {"remaining": 10}, feed_text="Apply now.")
    assert status == "success"  # _wire's _fetch_jd_text supplied the JD


def test_score_backlog_scores_newest_unscored_roles_up_to_the_limit(temp_db, monkeypatch):
    """Roles past the scan's per-run cap were never scored; the backlog job
    catches them up, newest first, within its own limit."""
    from app.background_jobs import score_backlog

    with get_db() as conn:
        ids = []
        for i in range(4):
            ids.append(conn.execute(
                "INSERT INTO jobs (company, job_title, url, jd_text, pipeline_stage, date_found) "
                "VALUES ('Acme', ?, ?, ?, 'discovered', strftime('%Y-%m-%dT%H:%M:%S','now', ?))",
                (f"Role {i}", f"https://boards.greenhouse.io/acme/jobs/{i}", _JD, f"-{i + 3} hours")).lastrowid)
        old = conn.execute(
            "INSERT INTO jobs (company, job_title, url, jd_text, pipeline_stage, date_found) "
            "VALUES ('Acme', 'Old', 'https://boards.greenhouse.io/acme/jobs/old', ?, 'discovered', "
            "strftime('%Y-%m-%dT%H:%M:%S','now','-30 days'))", (_JD,)).lastrowid
    _wire(monkeypatch, final_score=6.0)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.config.load_profile", lambda: {})

    counts = score_backlog(limit=2)

    assert counts.get("success") == 2
    with get_db() as conn:
        scored = {r[0] for r in conn.execute("SELECT id FROM jobs WHERE final_score IS NOT NULL")}
    assert scored == {ids[0], ids[1]}, "newest two first"
    assert old not in scored, "roles older than 14 days are left alone"


def test_score_backlog_gives_up_on_a_role_after_three_failures(temp_db, monkeypatch):
    from app.background_jobs import score_backlog
    with get_db() as conn:
        job = conn.execute(
            "INSERT INTO jobs (company, job_title, url, pipeline_stage, date_found) VALUES "
            "('Acme', 'No JD', 'https://boards.greenhouse.io/acme/jobs/x', 'discovered', "
            "strftime('%Y-%m-%dT%H:%M:%S','now','-5 hours'))").lastrowid
    _wire(monkeypatch)
    monkeypatch.setattr("app.jobs.fetch._fetch_jd_text", lambda url: "")  # never fetchable
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.config.load_profile", lambda: {})
    for _ in range(4):
        score_backlog(limit=5)
    with get_db() as conn:
        assert conn.execute("SELECT score_attempts FROM jobs WHERE id=?", (job,)).fetchone()[0] == 3


def test_score_backlog_leaves_roles_the_scan_just_found(temp_db, monkeypatch):
    from app.background_jobs import score_backlog
    with get_db() as conn:
        conn.execute("INSERT INTO jobs (company, job_title, url, jd_text, pipeline_stage, date_found) VALUES "
                     "('Acme', 'Fresh', 'https://boards.greenhouse.io/acme/jobs/f', ?, 'discovered', "
                     "strftime('%Y-%m-%dT%H:%M:%S','now','-10 minutes'))", (_JD,))
    _wire(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.config.load_profile", lambda: {})
    assert score_backlog(limit=5) == {}
