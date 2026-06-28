"""DB persistence for job records — upsert scored jobs and stubs into SQLite."""
import json
import logging
from datetime import date


_MAX_JSON_CHARS = 100_000


def _cap_json(data, label: str = "") -> str:
    """Serialize to JSON with a size cap — prevents oversized LLM output from bloating the DB."""
    s = json.dumps(data)
    if len(s) <= _MAX_JSON_CHARS:
        return s
    logging.warning("[save_job] LLM field '%s' is %d chars (limit %d) — capping", label, len(s), _MAX_JSON_CHARS)
    if isinstance(data, list):
        trimmed = data[:]
        while trimmed and len(json.dumps(trimmed)) > _MAX_JSON_CHARS:
            trimmed = trimmed[:-1]
        if not trimmed:
            logging.error(
                "[save_job] LLM field '%s' has a single item exceeding %d chars — "
                "storing empty list to preserve valid JSON",
                label, _MAX_JSON_CHARS,
            )
        return json.dumps(trimmed)
    logging.warning(
        "[save_job] LLM field '%s' is non-list (%s) and oversized — storing uncapped to preserve valid JSON",
        label, type(data).__name__,
    )
    return s


def save_stub_job_to_db(url: str, company: str = "") -> int:
    """Save a minimal stub record for a URL that failed JD fetch or parse."""
    from app.models import get_db, normalize_url
    from app.jobs.fetch import is_linkedin_job_url, extract_provisional_company
    today = str(date.today())
    norm_url = normalize_url(url)

    resolved_company = company or extract_provisional_company(url) or "Unknown"
    is_linkedin = is_linkedin_job_url(url)

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE source_url = ? OR (source_url IS NULL AND url = ?)",
            (norm_url, url),
        ).fetchone()
        if existing:
            return existing["id"]

        conn.execute(
            """INSERT INTO jobs
               (company, url, source_url, date_found, pipeline_stage, status, discovery_source,
                jd_fetch_attempts)
               VALUES (?,?,?,?,?,?,?,?)""",
            (resolved_company, url, norm_url, today, "identified", "Identified", "manual",
             3 if is_linkedin else 0),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return job_id


def save_job_to_db(url: str, score_record: dict) -> int:
    """Upsert job record into SQLite. Returns job_id."""
    from app.models import get_db, normalize_url
    today = str(date.today())
    norm_url = normalize_url(url)
    tech = score_record.get("tech_stack_detected", {})

    evidence = score_record.get("evidence", [])
    if not isinstance(evidence, list):
        logging.warning(f"Expected list for 'evidence', got {type(evidence)}. Coercing to [].")
        evidence = []

    mismatches = score_record.get("mismatches", [])
    if not isinstance(mismatches, list):
        logging.warning(f"Expected list for 'mismatches', got {type(mismatches)}. Coercing to [].")
        mismatches = []

    match_evidence = _cap_json(evidence, "match_evidence")
    match_mismatches = _cap_json(mismatches, "match_mismatches")
    match_bullets = _cap_json(score_record.get("tailored_bullets", []), "match_bullets")
    match_hooks = _cap_json(score_record.get("cover_letter_hooks", []), "match_hooks")
    differentiators = _cap_json(score_record.get("differentiator_themes", []), "differentiators")
    sections_to_drop = _cap_json(score_record.get("sections_to_drop", []), "sections_to_drop")
    role_archetype = score_record.get("role_archetype", "Other")

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE source_url = ? OR (source_url IS NULL AND url = ?)",
            (norm_url, url),
        ).fetchone()
        if existing:
            job_id = existing["id"]
            conn.execute(
                """UPDATE jobs SET
                   company=CASE WHEN COALESCE(company,'') IN ('','Unknown') AND ? != '' THEN ? ELSE company END,
                   job_title=CASE WHEN COALESCE(job_title,'') = '' AND ? != '' THEN ? ELSE job_title END,
                   final_score=?, deterministic_score=?, llm_adjustment=?,
                   auto_rejected=?, reject_reason=?, pros=?, cons=?,
                   greenfield=?, greenfield_rationale=?, pricing_model=?,
                   sector=?, recommended_angle=?, tech_stack_json=?,
                   flags_json=?, salary_range_detected=?, has_fde_model=?,
                   match_score=?, match_summary=?, match_evidence_json=?,
                   match_mismatches_json=?, match_bullets_json=?, match_hooks_json=?,
                   match_tailored_summary=?,
                   differentiator_themes_json=?, adjustment_weights_score=?,
                   posting_age_days=?, posting_date_raw=?, source_url=?, role_archetype=?,
                   interview_probability=?, interview_probability_rationale=?,
                   match_sections_to_drop_json=?,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    score_record.get("company") or "", score_record.get("company") or "",
                    score_record.get("job_title") or "", score_record.get("job_title") or "",
                    score_record.get("final_score"), score_record.get("deterministic_score"),
                    score_record.get("llm_adjustment"), int(score_record.get("auto_rejected", False)),
                    score_record.get("reject_reason"), score_record.get("pros"),
                    score_record.get("cons"), score_record.get("greenfield"),
                    score_record.get("greenfield_rationale"), score_record.get("pricing_model"),
                    score_record.get("sector"), score_record.get("recommended_angle"),
                    json.dumps(tech), json.dumps(score_record.get("flags", [])),
                    score_record.get("salary_range_detected"), score_record.get("has_fde_model"),
                    score_record.get("match_score"), score_record.get("match_summary"),
                    match_evidence, match_mismatches, match_bullets, match_hooks,
                    score_record.get("tailored_summary", "") or "",
                    differentiators, score_record.get("adjustment_weights_score"),
                    score_record.get("posting_age_days"), score_record.get("posting_date_raw"),
                    norm_url, role_archetype,
                    score_record.get("interview_probability"), score_record.get("interview_probability_rationale"),
                    sections_to_drop,
                    job_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO jobs
                   (company, job_title, url, date_found, final_score, deterministic_score,
                    llm_adjustment, auto_rejected, reject_reason, pros, cons, greenfield,
                    greenfield_rationale, pricing_model, sector, recommended_angle,
                    tech_stack_json, flags_json, salary_range_detected, has_fde_model,
                    match_score, match_summary, match_evidence_json, match_mismatches_json,
                    match_bullets_json, match_hooks_json, match_tailored_summary,
                    differentiator_themes_json,
                    adjustment_weights_score, posting_age_days, posting_date_raw, source_url, role_archetype,
                    interview_probability, interview_probability_rationale,
                    match_sections_to_drop_json, pipeline_stage, status, discovery_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    score_record.get("company", "Unknown"), score_record.get("job_title", ""),
                    url, today, score_record.get("final_score"),
                    score_record.get("deterministic_score"), score_record.get("llm_adjustment"),
                    int(score_record.get("auto_rejected", False)), score_record.get("reject_reason"),
                    score_record.get("pros"), score_record.get("cons"), score_record.get("greenfield"),
                    score_record.get("greenfield_rationale"), score_record.get("pricing_model"),
                    score_record.get("sector"), score_record.get("recommended_angle"),
                    json.dumps(tech), json.dumps(score_record.get("flags", [])),
                    score_record.get("salary_range_detected"), score_record.get("has_fde_model"),
                    score_record.get("match_score"), score_record.get("match_summary"),
                    match_evidence, match_mismatches, match_bullets, match_hooks,
                    score_record.get("tailored_summary", "") or "",
                    differentiators, score_record.get("adjustment_weights_score"),
                    score_record.get("posting_age_days"), score_record.get("posting_date_raw"),
                    norm_url, role_archetype,
                    score_record.get("interview_probability"), score_record.get("interview_probability_rationale"),
                    sections_to_drop,
                    "discovered", "discovered", "manual",
                ),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            """INSERT INTO score_history
               (job_id, final_score, deterministic_score, llm_adjustment,
                match_score, adjustment_weights_score)
               VALUES (?,?,?,?,?,?)""",
            (job_id, score_record.get("final_score"), score_record.get("deterministic_score"),
             score_record.get("llm_adjustment"), score_record.get("match_score"),
             score_record.get("adjustment_weights_score")),
        )
    return job_id
