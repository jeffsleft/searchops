"""
Unit tests for the job-notes service layer (app/services/job_actions.py):
add_job_note, get_recent_notes, find_jobs.

This is the single writer for job_notes — both the web "My notes" box
(job_save_notes in routes.py) and the Claude-Desktop MCP add_note tool call
these functions rather than writing SQL of their own. See test_notes_api_endpoint.py
for HTTP-level coverage (auth, JSON contract) and test_job_notes_migration.py
for the legacy-notes backfill.
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("NOTES_API_TOKEN", "test-notes-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ["DATABASE_PATH"] = _TMP_DB

import pytest

import app.config as config
import app.models as models

config.DATABASE_PATH = _TMP_DB
models.DATABASE_PATH = _TMP_DB

from app.models import get_db, init_db
from app.services.job_actions import add_job_note, get_recent_notes, find_jobs, MAX_NOTE_LENGTH


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


def _insert_job(**cols) -> int:
    cols.setdefault("date_found", "2026-06-13")
    fields = ", ".join(cols.keys())
    placeholders = ", ".join("?" for _ in cols)
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO jobs ({fields}) VALUES ({placeholders})", tuple(cols.values())
        )
        return cur.lastrowid


def _note_count(job_id: int) -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM job_notes WHERE job_id = ?", (job_id,)
        ).fetchone()[0]


# --- add_job_note ------------------------------------------------------------

def test_add_job_note_not_found():
    assert add_job_note(999999, "some text") == {"status": "not_found"}


def test_add_job_note_empty_text():
    job_id = _insert_job(company="Acme", job_title="RevOps Lead")
    assert add_job_note(job_id, "   ") == {"status": "empty"}
    assert _note_count(job_id) == 0


def test_add_job_note_too_long():
    job_id = _insert_job(company="Acme", job_title="RevOps Lead")
    result = add_job_note(job_id, "x" * (MAX_NOTE_LENGTH + 1))
    assert result == {"status": "too_long", "max_length": MAX_NOTE_LENGTH}
    assert _note_count(job_id) == 0


def test_add_job_note_at_max_length_ok():
    job_id = _insert_job(company="Acme", job_title="RevOps Lead")
    assert add_job_note(job_id, "x" * MAX_NOTE_LENGTH) == {"status": "ok"}
    assert _note_count(job_id) == 1


def test_add_job_note_writes_history_row_and_mirrors_jobs_notes():
    job_id = _insert_job(company="Synthesia", job_title="Director of RevOps")
    result = add_job_note(job_id, "Great call with the VP Eng today.", source="claude-desktop")
    assert result == {"status": "ok"}

    with get_db() as conn:
        row = conn.execute(
            "SELECT text, source FROM job_notes WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        job_row = conn.execute("SELECT notes FROM jobs WHERE id = ?", (job_id,)).fetchone()

    assert row["text"] == "Great call with the VP Eng today."
    assert row["source"] == "claude-desktop"
    assert job_row["notes"] == "Great call with the VP Eng today."


def test_add_job_note_appends_not_overwrites():
    job_id = _insert_job(company="Vercel", job_title="Head of GTM Ops")
    add_job_note(job_id, "First note.")
    add_job_note(job_id, "Second note.")
    assert _note_count(job_id) == 2
    notes = get_recent_notes(job_id, limit=5)
    assert [n["text"] for n in notes] == ["Second note.", "First note."]


# --- get_recent_notes ----------------------------------------------------

def test_get_recent_notes_respects_limit_and_order():
    job_id = _insert_job(company="Modal Labs", job_title="RevOps Manager")
    for i in range(7):
        add_job_note(job_id, f"note {i}")
    notes = get_recent_notes(job_id, limit=5)
    assert len(notes) == 5
    assert notes[0]["text"] == "note 6"  # newest first
    assert notes[-1]["text"] == "note 2"


def test_get_recent_notes_empty_for_job_with_no_notes():
    job_id = _insert_job(company="NoNotesCo", job_title="Some Role")
    assert get_recent_notes(job_id) == []


# --- find_jobs -------------------------------------------------------------

def test_find_jobs_exact_match():
    job_id = _insert_job(company="Synthesia", job_title="Director of RevOps")
    matches = find_jobs("Synthesia")
    assert any(m["job_id"] == job_id for m in matches)
    top = matches[0]
    assert set(top.keys()) == {"job_id", "company", "title", "status", "updated_at"}
    assert top["company"] == "Synthesia"


def test_find_jobs_ambiguous_returns_multiple_candidates_and_writes_nothing():
    id_a = _insert_job(company="Ramp Financial", job_title="RevOps Lead")
    id_b = _insert_job(company="Ramp Network", job_title="GTM Ops Manager")
    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0]

    matches = find_jobs("Ramp")

    ids = {m["job_id"] for m in matches}
    assert {id_a, id_b}.issubset(ids)

    with get_db() as conn:
        after = conn.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0]
    assert after == before  # search never writes


def test_find_jobs_unknown_target_returns_empty_and_writes_nothing():
    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0]

    matches = find_jobs("Zzyzx Nonexistent Company Q9")

    assert matches == []
    with get_db() as conn:
        after = conn.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0]
    assert after == before


def test_find_jobs_blank_query_returns_empty():
    assert find_jobs("") == []
    assert find_jobs("   ") == []


def test_find_jobs_tolerates_a_typo():
    job_id = _insert_job(company="Anthropic", job_title="Head of RevOps")
    matches = find_jobs("Anthropci")  # transposed letters
    assert any(m["job_id"] == job_id for m in matches)
