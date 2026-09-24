"""One stage vocabulary (app/pipeline/tracker.py STAGES).

Every screen reads stage labels from the registry. These tests fail if a
template grows its own copy again, and pin the separate 'dismissed' stage.
"""
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.auth import SESSION_COOKIE, create_session_token
from app.models import get_db, init_db
from app.pipeline.tracker import STAGES, TERMINAL_STAGES, stage_label
from app.routes import create_app

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
HX = {"X-Requested-With": "XMLHttpRequest", "HX-Request": "true"}


def test_no_template_hardcodes_a_stage_label_map():
    # A code-and-label pair like ('hm_interview', 'HM Interview') or
    # 'hm_interview': 'HM Interview' means a template keeps its own copy.
    pairs = [(code, s["label"]) for code, s in STAGES.items() if code != s["label"].lower()]
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text()
        for code, label in pairs:
            if re.search(rf"""['"]{code}['"]\s*[,:]\s*['"]{re.escape(label)}['"]""", text):
                offenders.append(f"{path.relative_to(TEMPLATES)}: {code} -> {label}")
    assert not offenders, "use STAGES / stage_label instead:\n" + "\n".join(offenders)


def test_guide_and_settings_list_only_real_stages():
    for name in ("guide.html", "settings.html"):
        text = (TEMPLATES / name).read_text()
        for fake in ("Watchlist → Researching", "('Screening',", "('Interviewing',"):
            assert fake not in text, f"{name} still teaches the made-up stage list"


def test_registry_basics():
    assert "dismissed" in TERMINAL_STAGES
    assert stage_label("dismissed") == "Dismissed"
    assert stage_label(None) == "—"
    assert all(s.get("desc") for s in STAGES.values()), "every stage needs a one-line description"


@pytest.fixture()
def client():
    c = TestClient(create_app())
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


def _job(stage="discovered", applied_at=None):
    with get_db() as conn:
        return conn.execute(
            "INSERT INTO jobs (company, job_title, pipeline_stage, applied_at, date_found) "
            "VALUES ('Acme', 'Dir RevOps', ?, ?, date('now'))", (stage, applied_at)).lastrowid


def test_dismiss_button_uses_its_own_stage(client):
    job_id = _job()
    client.post(f"/job/{job_id}/dismiss", headers=HX)
    with get_db() as conn:
        assert conn.execute("SELECT pipeline_stage FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "dismissed"


def test_migration_moves_only_button_dismissals_that_never_applied():
    tagged, real_decline, applied = _job("i_declined"), _job("i_declined"), _job("i_declined", "2026-07-01")
    with get_db() as conn:
        for jid, note in ((tagged, "Decline reason: dismissed_from_discovery"),
                          (real_decline, "Decline reason: Requires in-office"),
                          (applied, "Decline reason: dismissed_from_discovery")):
            conn.execute("INSERT INTO pipeline_history (job_id, from_stage, to_stage, notes) "
                         "VALUES (?, 'discovered', 'i_declined', ?)", (jid, note))
    init_db()
    init_db()  # idempotent
    with get_db() as conn:
        stage = {jid: conn.execute("SELECT pipeline_stage FROM jobs WHERE id=?", (jid,)).fetchone()[0]
                 for jid in (tagged, real_decline, applied)}
        moves = conn.execute("SELECT COUNT(*) FROM pipeline_history WHERE job_id=? AND to_stage='dismissed'",
                             (tagged,)).fetchone()[0]
    assert stage == {tagged: "dismissed", real_decline: "i_declined", applied: "i_declined"}
    assert moves == 1


def test_job_page_shows_the_stage_badge_and_a_real_next_step(client):
    job_id = _job("recruiter")
    with get_db() as conn:
        conn.execute("UPDATE jobs SET final_score = 7.5 WHERE id = ?", (job_id,))
    html = client.get(f"/job/{job_id}").text
    assert '<span class="stage recruiter">Recruiter Screen</span>' in html
    assert "Prep your talking points" in html


def test_migration_leaves_a_later_real_decline_alone():
    # Dismissed once, reopened, then declined for a real reason.
    job_id = _job("i_declined")
    with get_db() as conn:
        conn.execute("INSERT INTO pipeline_history (job_id, from_stage, to_stage, notes) "
                     "VALUES (?, 'discovered', 'i_declined', 'Decline reason: dismissed_from_discovery')", (job_id,))
        conn.execute("INSERT INTO pipeline_history (job_id, from_stage, to_stage, notes) "
                     "VALUES (?, 'i_declined', 'researching', NULL)", (job_id,))
        conn.execute("INSERT INTO pipeline_history (job_id, from_stage, to_stage, notes) "
                     "VALUES (?, 'researching', 'i_declined', 'Decline reason: Requires in-office')", (job_id,))
    init_db()
    with get_db() as conn:
        assert conn.execute("SELECT pipeline_stage FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "i_declined"
