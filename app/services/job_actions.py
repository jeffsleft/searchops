"""
Job-action business logic extracted from routes.py.

These functions own the "do the work" half of job-related handlers: scoring a
brand-new job from input, fetching+scoring a stub, and persisting a pipeline
stage change. Handlers parse the request, call one of these, then map the
returned result dict to HTML. Keeping the logic here means a fix lands once and
every caller gets it (outcome_charter KR1).
"""

from app.config import HIGH_SCORE_THRESHOLD
from app.models import get_db, normalize_url
from app.pipeline.tracker import STAGES
from app.scoring.research import score_job
from app.jobs.fetch import _fetch_jd_text, is_linkedin_job_url
from app.jobs.persist import save_job_to_db
from app.services.scoring_service import score_job_from_url_and_persist


def score_new_job_from_input(url: str, jd_text: str) -> dict:
    """Score a brand-new job from a URL and/or pasted JD text (Dashboard "Score a Job").

    Dedup by URL before scoring; fetch the JD when only a URL is supplied; persist
    and fire the high-score Slack alert when a URL anchors the row.

    Returns one of:
      {"status": "missing_input"}
      {"status": "duplicate", "job_id": int, "company": str}
      {"status": "fetch_failed"}
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
    if url:
        score_record["_url"] = url
        job_id = save_job_to_db(url, score_record)
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
      {"status": "not_found"}
      {"status": "ok", "job": dict, "promoted": bool, "stage_label": str}
    """
    if new_stage not in STAGES:
        return {"status": "invalid_stage"}

    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET pipeline_stage=?, status=?, auto_rejected=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_stage, STAGES[new_stage]["label"], job_id),
        )
        conn.execute(
            "INSERT INTO pipeline_history (job_id, to_stage, changed_by) VALUES (?,?,?)",
            (job_id, new_stage, "jeff"),
        )
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
