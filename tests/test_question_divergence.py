"""
Tests for check_question_divergence (app/pipeline/prep.py), rebuilt on the
per-session model 2026-07-21 after the original flat-table version
(app/questions/bank.py's _check_divergence) was found to be orphaned --
its only caller had no route registration and was deleted rather than revived.

Matches on session_questions_to_ask.text scoped to the same job (via
interview_sessions), across rows with a different persona and a non-empty
answer -- the direct successor to the old asked_to-based matching.
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ["DATABASE_PATH"] = _TMP_DB

import json
import pytest
from starlette.testclient import TestClient

from app.models import init_db, get_db
from app.routes import create_app
from app.auth import create_session_token, SESSION_COOKIE
from app.pipeline.prep import check_question_divergence


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


def _make_job_with_two_sessions(conn):
    conn.execute(
        """INSERT INTO jobs (company, job_title, pipeline_stage, status, date_found, discovery_source)
           VALUES ('Forgeline', 'Director of RevOps', 'identified', 'Identified', '2026-07-21', 'manual')"""
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    session_ids = []
    for type_id, label in [("recruiter", "Recruiter Screen"), ("hm", "HM Interview")]:
        conn.execute(
            """INSERT INTO interview_sessions (job_id, type_id, label, position, created_at, updated_at)
               VALUES (?, ?, ?, 0, '2026-07-21T00:00:00+00:00', '2026-07-21T00:00:00+00:00')""",
            (job_id, type_id, label),
        )
        session_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return job_id, session_ids


def test_no_divergence_check_without_an_answer(client):
    """A question with no answer yet must not trigger a divergence check (no LLM call)."""
    with get_db() as conn:
        job_id, (s1, _) = _make_job_with_two_sessions(conn)
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona) VALUES (?, ?, ?)",
            (s1, "What is your NRR?", "CFO"),
        )
        q_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # No provider stub -- if this called get_provider().generate_json it would
        # raise on the fake API key. Its absence of a crash proves it short-circuited.
        check_question_divergence(conn, q_id)

        row = conn.execute("SELECT divergence_flag FROM session_questions_to_ask WHERE id = ?", (q_id,)).fetchone()
    assert row["divergence_flag"] in (0, None)


def test_no_divergence_with_only_one_answered_persona(client):
    """Only one persona has answered -- no sibling to compare against, no flag."""
    with get_db() as conn:
        job_id, (s1, _) = _make_job_with_two_sessions(conn)
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona, answer) VALUES (?, ?, ?, ?)",
            (s1, "What is your NRR?", "CFO", "About 110%."),
        )
        q_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        check_question_divergence(conn, q_id)

        row = conn.execute("SELECT divergence_flag FROM session_questions_to_ask WHERE id = ?", (q_id,)).fetchone()
    assert row["divergence_flag"] in (0, None)


def test_divergence_flags_both_rows_when_personas_disagree(client, monkeypatch):
    """Same question text, same job, two different personas, both answered --
    must call the LLM and flag both rows when the result says not aligned."""
    with get_db() as conn:
        job_id, (s1, s2) = _make_job_with_two_sessions(conn)
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona, answer) VALUES (?, ?, ?, ?)",
            (s1, "What is your NRR?", "CFO", "About 110%, trending up."),
        )
        q_cfo = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona, answer) VALUES (?, ?, ?, ?)",
            (s2, "What is your NRR?", "CRO", "Honestly not sure, maybe 90%."),
        )
        q_cro = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        class FakeProvider:
            def generate_json(self, prompt):
                assert "Forgeline" in prompt
                assert "What is your NRR?" in prompt
                return {"aligned": False, "summary": "Numbers disagree.", "red_flag": True, "red_flag_reason": "Inconsistent metrics."}

        monkeypatch.setattr("app.providers.get_provider", lambda: FakeProvider())

        check_question_divergence(conn, q_cro)

        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, divergence_flag, divergence_notes FROM session_questions_to_ask WHERE id IN (?, ?)",
            (q_cfo, q_cro),
        ).fetchall()}

    assert rows[q_cfo]["divergence_flag"] == 1
    assert rows[q_cro]["divergence_flag"] == 1
    assert json.loads(rows[q_cfo]["divergence_notes"])["summary"] == "Numbers disagree."


def test_same_persona_answering_twice_does_not_self_trigger(client):
    """Two rows with the SAME persona must not be treated as a divergence pair."""
    with get_db() as conn:
        job_id, (s1, s2) = _make_job_with_two_sessions(conn)
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona, answer) VALUES (?, ?, ?, ?)",
            (s1, "How large is the team?", "CFO", "About 12 people."),
        )
        q1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona, answer) VALUES (?, ?, ?, ?)",
            (s2, "How large is the team?", "CFO", "About 12 people."),
        )
        q2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        check_question_divergence(conn, q2)

        rows = conn.execute(
            "SELECT divergence_flag FROM session_questions_to_ask WHERE id IN (?, ?)", (q1, q2)
        ).fetchall()
    assert all(r["divergence_flag"] in (0, None) for r in rows)


def test_question_to_ask_update_route_triggers_divergence_check(client, monkeypatch):
    """End-to-end: PATCHing an answer through the live route fires the check."""
    with get_db() as conn:
        job_id, (s1, s2) = _make_job_with_two_sessions(conn)
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona, answer) VALUES (?, ?, ?, ?)",
            (s1, "What is your pricing model?", "CFO", "Seat-based."),
        )
        q_cfo = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, persona) VALUES (?, ?, ?)",
            (s2, "What is your pricing model?", "CRO"),
        )
        q_cro = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    class FakeProvider:
        def generate_json(self, prompt):
            return {"aligned": False, "summary": "One says seat-based, the other consumption-based.", "red_flag": False, "red_flag_reason": None}

    monkeypatch.setattr("app.providers.get_provider", lambda: FakeProvider())

    resp = client.patch(
        f"/prep/questions-to-ask/{q_cro}",
        data={"answer": "Consumption-based, I think."},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200, resp.text

    with get_db() as conn:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, divergence_flag FROM session_questions_to_ask WHERE id IN (?, ?)",
            (q_cfo, q_cro),
        ).fetchall()}
    assert rows[q_cfo]["divergence_flag"] == 1
    assert rows[q_cro]["divergence_flag"] == 1
