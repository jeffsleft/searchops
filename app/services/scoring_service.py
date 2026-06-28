"""
Job scoring business logic extracted from routes.py.
Implements the four main scoring workflows used across handlers.
"""
import json
import logging
from datetime import date

from app.config import HIGH_SCORE_THRESHOLD
from app.models import get_db, normalize_url
from app.scoring.research import score_job
from app.jobs.fetch import _fetch_jd_text, is_linkedin_job_url
from app.jobs.persist import save_job_to_db, _cap_json


def persist_score_record_to_job(job_id: int, score_record: dict, jd_text: str = "", transition_stage: bool = False) -> None:
    """Write a score_record dict to a job row in the database.

    Used when we have a fully-scored job record and need to persist it by job_id.
    Handles both new inserts and updates. Uses exact SQL column mapping from routes.py.

    Args:
        job_id: Database job ID
        score_record: Fully-scored job record dict
        jd_text: Raw JD text to store (will be capped at 30k chars)
        transition_stage: If True, transition 'identified' jobs to 'discovered'
    """
    tech = score_record.get("tech_stack_detected", {})
    evidence = score_record.get("evidence", []) if isinstance(score_record.get("evidence"), list) else []
    mismatches = score_record.get("mismatches", []) if isinstance(score_record.get("mismatches"), list) else []
    role_archetype = score_record.get("role_archetype", "Other")

    with get_db() as conn:
        # Check if this job already exists
        existing = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()

        if existing:
            # Update existing row (score fields only, no pipeline_stage yet)
            conn.execute(
                """UPDATE jobs SET
                   company=CASE WHEN COALESCE(company,'') IN ('','Unknown') AND ? != '' THEN ? ELSE company END,
                   job_title=CASE WHEN COALESCE(job_title,'') IN ('','Untitled role') AND ? != '' THEN ? ELSE job_title END,
                   final_score=?, deterministic_score=?, llm_adjustment=?,
                   auto_rejected=?, reject_reason=?, pros=?, cons=?,
                   greenfield=?, sector=?, salary_range_detected=?,
                   match_score=?, match_summary=?, match_evidence_json=?,
                   match_mismatches_json=?, match_bullets_json=?, match_hooks_json=?,
                   match_tailored_summary=?,
                   differentiator_themes_json=?, adjustment_weights_score=?,
                   posting_age_days=?, posting_date_raw=?, tech_stack_json=?,
                   flags_json=?, role_archetype=?,
                   interview_probability=?, interview_probability_rationale=?,
                   match_sections_to_drop_json=?,
                   jd_text=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    score_record.get("company") or "", score_record.get("company") or "",
                    score_record.get("job_title") or "", score_record.get("job_title") or "",
                    score_record.get("final_score"), score_record.get("deterministic_score"),
                    score_record.get("llm_adjustment"), int(score_record.get("auto_rejected", False)),
                    score_record.get("reject_reason"), score_record.get("pros"), score_record.get("cons"),
                    score_record.get("greenfield"), score_record.get("sector"),
                    score_record.get("salary_range_detected"),
                    score_record.get("match_score"), score_record.get("match_summary"),
                    _cap_json(evidence, "match_evidence"), _cap_json(mismatches, "match_mismatches"),
                    _cap_json(score_record.get("tailored_bullets", []), "match_bullets"),
                    _cap_json(score_record.get("cover_letter_hooks", []), "match_hooks"),
                    score_record.get("tailored_summary", "") or "",
                    _cap_json(score_record.get("differentiator_themes", []), "differentiators"),
                    score_record.get("adjustment_weights_score"),
                    score_record.get("posting_age_days"), score_record.get("posting_date_raw"),
                    json.dumps(tech), json.dumps(score_record.get("flags", [])),
                    role_archetype,
                    score_record.get("interview_probability"),
                    score_record.get("interview_probability_rationale"),
                    _cap_json(score_record.get("sections_to_drop", []), "sections_to_drop"),
                    jd_text[:30000] if jd_text else "",
                    job_id,
                ),
            )

            # If transition_stage is True, transition identified jobs to discovered
            if transition_stage:
                conn.execute(
                    "UPDATE jobs SET pipeline_stage='discovered' WHERE id=? AND pipeline_stage='identified'",
                    (job_id,),
                )


def score_job_from_text_and_persist(job_id: int, jd_text: str, url: str = "", transition_stage: bool = False) -> dict:
    """Score a job from pasted or fetched JD text and persist to DB by job_id.

    Args:
        job_id: Database job ID
        jd_text: Raw job description text
        url: Optional URL (for logging)
        transition_stage: If True, transition 'identified' jobs to 'discovered'

    Returns:
        dict with keys: status, job_id, score, error (if failed)
    """
    if not jd_text or len(jd_text) < 100:
        return {
            "status": "error",
            "error": "JD text too short",
        }

    try:
        score_record = score_job(jd_text[:30000])
        persist_score_record_to_job(job_id, score_record, jd_text, transition_stage=transition_stage)

        # Check if high-score alert needed
        if score_record.get("final_score", 0) >= HIGH_SCORE_THRESHOLD:
            from app.notifications.slack import send_high_score_alert
            send_high_score_alert(job_id, score_record)

        return {
            "status": "success",
            "job_id": job_id,
            "score": score_record.get("final_score"),
        }
    except Exception as e:
        logging.error("score_job_from_text_and_persist failed for job %s: %s", job_id, e)
        return {
            "status": "error",
            "job_id": job_id,
            "error": str(e)[:120],
        }


def score_job_from_url_and_persist(job_id: int, url: str) -> dict:
    """Fetch JD text from a URL and score it, persisting to DB by job_id.

    Returns:
        dict with keys: status, job_id, score, error (if failed)
    """
    # Check if this is a LinkedIn URL (cannot be fetched)
    if is_linkedin_job_url(url):
        return {
            "status": "error",
            "job_id": job_id,
            "error": "LinkedIn blocks automated fetching. Use the Paste JD text form.",
        }

    try:
        jd_text = _fetch_jd_text(url)
        if not jd_text:
            return {
                "status": "error",
                "job_id": job_id,
                "error": "Could not fetch JD from this URL.",
            }

        return score_job_from_text_and_persist(job_id, jd_text, url)
    except Exception as e:
        logging.error("score_job_from_url_and_persist failed for job %s: %s", job_id, e)
        return {
            "status": "error",
            "job_id": job_id,
            "error": str(e)[:120],
        }


def handle_job_add_with_optional_scoring(
    url: str,
    job_title: str = "",
    company: str = "",
    skip_score: bool = False,
) -> dict:
    """Add a new job (any URL type) with optional scoring and dedup.

    Args:
        url: Job posting URL (LinkedIn, Greenhouse, Lever, Ashby, etc.)
        job_title: Optional title override
        company: Optional company name override
        skip_score: If True, insert as they_declined without scoring

    Returns:
        dict with keys: status, job_id, score (if scored), error (if failed)
    """
    if not url:
        return {
            "status": "error",
            "error": "URL is required",
        }

    from app.security.url_guard import validate_url

    try:
        url = validate_url(url)
    except ValueError as e:
        return {
            "status": "error",
            "error": f"Invalid URL: {str(e)}",
        }

    # Dedup check
    norm_url = normalize_url(url)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, company, job_title, pipeline_stage FROM jobs WHERE source_url = ? OR (source_url IS NULL AND url = ?)",
            (norm_url, url),
        ).fetchone()

    if existing:
        return {
            "status": "duplicate",
            "job_id": existing["id"],
            "company": existing["company"],
            "job_title": existing["job_title"] or "Untitled",
            "pipeline_stage": existing["pipeline_stage"],
        }

    # Insert stub
    today = str(date.today())
    initial_stage = "they_declined" if skip_score else "identified"
    initial_status = "They Declined" if skip_score else "Identified"
    disc_source = "linkedin" if is_linkedin_job_url(url) else "manual"

    with get_db() as conn:
        conn.execute(
            """INSERT INTO jobs
               (company, job_title, url, source_url, date_found, pipeline_stage, status, discovery_source)
               VALUES (?,?,?,?,?,?,?,?)""",
            (company or "Unknown", job_title or None, url, norm_url, today,
             initial_stage, initial_status, disc_source),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Declined fast-path: no scoring needed
    if skip_score:
        return {
            "status": "declined_fast_path",
            "job_id": job_id,
            "company": company or "Job",
        }

    # Try to fetch and score
    try:
        jd_text = _fetch_jd_text(url)
        if not jd_text:
            return {
                "status": "added_no_fetch",
                "job_id": job_id,
                "message": "Added but couldn't fetch JD — paste manually in Discovered view",
            }

        score_record = score_job(jd_text)
        persisted_job_id = save_job_to_db(url, score_record)

        # High-score alert — use the job_id returned from save_job_to_db, not the stub
        if score_record.get("final_score", 0) >= HIGH_SCORE_THRESHOLD:
            from app.notifications.slack import send_high_score_alert
            send_high_score_alert(persisted_job_id, score_record)

        return {
            "status": "scored",
            "job_id": persisted_job_id,
            "score": score_record.get("final_score", 0),
        }
    except Exception as e:
        logging.error("handle_job_add_with_optional_scoring score failed for job %s: %s", job_id, e)
        return {
            "status": "added_score_failed",
            "job_id": job_id,
            "error": str(e)[:120],
        }
