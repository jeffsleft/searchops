"""
Job detail data aggregation service extracted from routes.py.

Owns job detail queries, flag processing, question aggregation, and enrichment.
Returns a dict ready for template rendering.
"""
from app.models import get_db


FLAGS = {
    "windows_penalty":    ("Windows + Teams stack",          -2.0),
    "modern_tech":        ("Mac + Slack stack",              +1.0),
    "experienced_founder":("2nd-time founder",              +1.0),
    "first_time_founder": ("1st-time founder",              -1.0),
    "greenfield":         ("Greenfield / 0→1 opportunity",  +2.0),
    "process_upcycling":  ("Process cleanup only",          -1.0),
    "modern_pricing":     ("Consumption/Outcome pricing",   +1.0),
    "target_sector":      ("Target sector match",           +1.5),
    "moderate_sector":    ("Moderate sector match",         +0.5),
    "low_interest_sector":("Low-interest sector",           -0.5),
    "remote":             ("Fully remote",                  +0.5),
    "churn_burn":         ("CS shrinking, Sales growing",   -1.0),
    "cfo_cro_warning":    ("CFO/CRO tension signal",         0.0),
    "low_runway_warning": ("Low runway (<18 mo)",            0.0),
}


def build_job_detail_data(job_id: int, _enrich_job_fn) -> dict | None:
    """
    Build all job detail data: job record, contacts, questions, flags, tech stack.

    Args:
        job_id: Database job ID
        _enrich_job_fn: Callable to enrich a job dict (from routes._enrich_job)

    Returns:
        Dict with keys: job, contacts, questions, questions_answered, flags_fired, tech_stack
        Or None if job not found
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None

        contacts = conn.execute("SELECT * FROM contacts WHERE job_id = ?", (job_id,)).fetchall()
        questions = conn.execute(
            "SELECT * FROM questions WHERE job_id = ? ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, created_at",
            (job_id,)
        ).fetchall()

    job = _enrich_job_fn(dict(row))

    flags_fired = [
        {"id": fid, "label": FLAGS[fid][0], "weight": FLAGS[fid][1]}
        for fid in job.get("flags_list", [])
        if fid in FLAGS
    ]

    questions_list = [dict(q) for q in questions]
    questions_answered = sum(1 for q in questions_list if q.get("status") == "answered")

    tech_stack = job.get("tech_stack", {})

    return {
        'job': job,
        'contacts': [dict(c) for c in contacts],
        'questions': questions_list,
        'questions_answered': questions_answered,
        'flags_fired': flags_fired,
        'tech_stack': tech_stack,
    }
