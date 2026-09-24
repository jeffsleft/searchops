"""
Job-action business logic extracted from routes.py.

These functions own the "do the work" half of job-related handlers: scoring a
brand-new job from input, fetching+scoring a stub, persisting a pipeline stage
change, and reading/writing job notes. Handlers parse the request, call one of
these, then map the returned result dict to HTML or JSON. Keeping the logic
here means a fix lands once and every caller gets it (outcome_charter KR1) —
add_job_note() in particular is the single writer for job_notes, called by
both the web "My notes" box and the Claude-Desktop MCP server, so a note never
gets written through a second, divergent path.
"""

import difflib
import logging

from app.config import HIGH_SCORE_THRESHOLD
from app.models import get_db, normalize_url
from app.pipeline.tracker import STAGES, advance_stage
from app.scoring.research import score_job
from app.jobs.fetch import _fetch_jd_text, is_linkedin_job_url
from app.jobs.persist import save_job_to_db
from app.services.scoring_service import (
    score_job_from_url_and_persist, score_job_from_text_and_persist,
)

MAX_NOTE_LENGTH = 5000


def score_new_job_from_input(url: str, jd_text: str) -> dict:
    """Score a brand-new job from a URL and/or pasted JD text (Dashboard "Score a Job").

    Dedup by URL before scoring; fetch the JD when only a URL is supplied; persist
    and fire the high-score Slack alert when a URL anchors the row.

    Returns one of:
      {"status": "missing_input"}
      {"status": "duplicate", "job_id": int, "company": str}
      {"status": "fetch_failed"}
      {"status": "insufficient"}
      {"status": "scored", "score_record": dict}
    """
    url = (url or "").strip()
    jd_text = (jd_text or "").strip()
    if not url and not jd_text:
        return {"status": "missing_input"}

    if url:
        norm_url = normalize_url(url)
        if norm_url:
            with get_db() as conn:
                existing = conn.execute(
                    "SELECT id, company FROM jobs WHERE source_url = ? OR (source_url IS NULL AND url = ?)",
                    (norm_url, url),
                ).fetchone()
            if existing:
                return {"status": "duplicate", "job_id": existing["id"], "company": existing["company"]}

    if url and not jd_text:
        jd_text = _fetch_jd_text(url) or ""
        if not jd_text:
            return {"status": "fetch_failed"}

    score_record = score_job(jd_text)

    # Same jd_insufficient guard as score_job_from_text_and_persist (scoring_service.py)
    # and the stub-retry loop (fetch.py) — without it, thin/blocked content (e.g. a
    # bot-detection placeholder page) gets silently saved as a real job with
    # company="Unknown" and no title instead of surfacing as a failure.
    if score_record.get("jd_insufficient"):
        return {"status": "insufficient"}

    if url:
        score_record["_url"] = url
        job_id = save_job_to_db(url, score_record, jd_text=jd_text)
        score_record["_job_id"] = job_id
        if score_record.get("final_score", 0) >= HIGH_SCORE_THRESHOLD:
            from app.notifications.slack import send_high_score_alert
            send_high_score_alert(job_id, score_record)

    return {"status": "scored", "score_record": score_record}


def fetch_and_score_stub(job_id: int) -> dict:
    """Fetch the JD and run full scoring for a stub job (pipeline_stage='identified').

    Manages the jd_fetch_attempts counter: increment before the attempt, reset to
    0 on success. LinkedIn URLs short-circuit because they block automated fetching.

    Returns one of:
      {"status": "not_found"}
      {"status": "no_url"}
      {"status": "linkedin_blocked"}
      {"status": "error", "attempts": int, "error": str}
      {"status": "scored", "score": float | None}
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {"status": "not_found"}
    job = dict(row)
    url = job.get("url", "")
    if not url:
        return {"status": "no_url"}
    if is_linkedin_job_url(url):
        return {"status": "linkedin_blocked"}

    attempts = job.get("jd_fetch_attempts") or 0
    with get_db() as conn:
        conn.execute("UPDATE jobs SET jd_fetch_attempts = ? WHERE id = ?", (attempts + 1, job_id))

    result = score_job_from_url_and_persist(job_id, url)
    if result["status"] == "error":
        return {"status": "error", "attempts": attempts + 1, "error": result.get("error", "Unknown error")}

    with get_db() as conn:
        conn.execute("UPDATE jobs SET jd_fetch_attempts = 0 WHERE id = ?", (job_id,))
    return {"status": "scored", "score": result.get("score")}


def update_job_stage(job_id: int, new_stage: str) -> dict:
    """Persist a pipeline stage change from the detail panel and log history.

    Returns one of:
      {"status": "invalid_stage"}
      {"status": "needs_reason"}   (decline-type stages go through the dialog)
      {"status": "not_found"}
      {"status": "ok", "job": dict, "promoted": bool, "stage_label": str}
    """
    from app.services.pipeline_service import record_stage_change

    if new_stage not in STAGES:
        return {"status": "invalid_stage"}
    # Declines, closures and duplicates need a reason on record; this quick
    # dropdown has no reason field, so those go through the job page's dialog.
    from app.pipeline.tracker import REASON_REQUIRED_STAGES
    if new_stage in REASON_REQUIRED_STAGES:
        return {"status": "needs_reason"}

    with get_db() as conn:
        # Update job status and auto_rejected fields
        conn.execute(
            "UPDATE jobs SET status=?, auto_rejected=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (STAGES[new_stage]["label"], job_id),
        )
        # Route pipeline_stage change through the single sanctioned writer
        record_stage_change(conn, job_id, new_stage, note=None, changed_by="jeff")
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    if not row:
        return {"status": "not_found"}

    promoted = new_stage not in ("discovered", "identified")
    return {
        "status": "ok",
        "job": dict(row),
        "promoted": promoted,
        "stage_label": STAGES[new_stage]["label"],
    }


def add_job_note(job_id: int, text: str, source: str = "web") -> dict:
    """Append one note to job_notes. The single writer for job notes — both the
    web "My notes" box (job_save_notes) and the Claude-Desktop MCP add_note
    tool call this, never SQL of their own (see module docstring).

    Mirrors the note's text into jobs.notes (legacy single-value column) so the
    dashboard/pipeline 📝 preview keeps showing the latest note without needing
    its own job_notes-aware query.

    Returns one of:
      {"status": "not_found"}
      {"status": "empty"}
      {"status": "too_long", "max_length": int}
      {"status": "ok"}
    """
    text = (text or "").strip()
    if not text:
        return {"status": "empty"}
    if len(text) > MAX_NOTE_LENGTH:
        return {"status": "too_long", "max_length": MAX_NOTE_LENGTH}

    with get_db() as conn:
        job = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return {"status": "not_found"}
        conn.execute(
            "INSERT INTO job_notes (job_id, text, source) VALUES (?, ?, ?)",
            (job_id, text, source),
        )
        conn.execute(
            "UPDATE jobs SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (text, job_id),
        )
    return {"status": "ok"}


def get_recent_notes(job_id: int, limit: int = 5) -> list[dict]:
    """Most recent notes for a job, newest first.

    limit is clamped to [1, 50] — SQLite treats a non-positive LIMIT as
    "unlimited", not zero, so an unclamped caller-supplied limit (this is
    reachable from the /api/notes/ query param) could return a job's entire
    note history instead of a bounded page.
    """
    limit = max(1, min(int(limit), 50))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, text, source, created_at FROM job_notes "
            "WHERE job_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def find_jobs(query: str, limit: int = 5) -> list[dict]:
    """Fuzzy match on company + job_title (stdlib difflib — no fuzzy-match
    dependency for a search over a few hundred rows). Read-only; never writes.

    Returns candidates ordered best-match-first:
      [{"job_id", "company", "title", "status", "updated_at"}, ...]
    """
    query = (query or "").strip()
    if not query:
        return []
    q = query.lower()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, company, job_title, status, updated_at FROM jobs"
        ).fetchall()

    scored = []
    for row in rows:
        company = (row["company"] or "").lower()
        title = (row["job_title"] or "").lower()
        if q in company or q in title:
            score = 0.99  # substring hit outranks any fuzzy-only ratio
        else:
            # Ratio against each field separately, not the concatenated
            # "company title" string — matching against the full haystack let
            # short unrelated queries pick up enough scattered single-character
            # overlap (shared letters/spaces) to clear a low cutoff. Per-field
            # ratio plus a stricter cutoff keeps real near-misses (typos) while
            # rejecting queries that share no real substring with either field.
            score = max(
                difflib.SequenceMatcher(None, q, company).ratio(),
                difflib.SequenceMatcher(None, q, title).ratio(),
            )
        if score >= 0.6:
            scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "job_id": row["id"],
            "company": row["company"],
            "title": row["job_title"],
            "status": row["status"],
            "updated_at": row["updated_at"],
        }
        for _, row in scored[:limit]
    ]


def promote_job_from_discovery(job_id: int) -> dict:
    """Move a discovered job to 'identified' and score it via the canonical path.

    Scores from the job's URL first, falling back to pasted jd_text. A scoring
    failure doesn't block the promotion — the stage change already happened.

    Returns one of:
      {"status": "not_found"}
      {"status": "stage_error", "error": str}
      {"status": "ok"}
    """
    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        return {"status": "not_found"}
    job = dict(job)

    result = advance_stage(job_id, "identified", notes="Promoted from discovery")
    if not result["ok"]:
        return {"status": "stage_error", "error": result["error"]}

    # Trigger scoring via the canonical service path (persists the full score
    # record — evidence, mismatches, bullets, hooks — and handles the alert).
    # Stage was already advanced above, so no transition_stage here.
    score_result = {"status": "skipped"}
    if job.get("url"):
        score_result = score_job_from_url_and_persist(job_id, job["url"])
    if score_result.get("status") != "success" and (job.get("jd_text") or "").strip():
        score_result = score_job_from_text_and_persist(job_id, job["jd_text"])
    if score_result.get("status") not in ("success", "skipped"):
        logging.error("Scoring failed during promotion of job %s: %s",
                      job_id, score_result.get("error"))

    return {"status": "ok"}
