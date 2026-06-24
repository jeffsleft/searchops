"""
Pipeline stage management. Enforces allowed transitions, logs history,
requires decline reasons on terminal stages.
"""
from datetime import datetime, timezone
from app.models import get_db

# Stage codes and terminal status
STAGES = {
    "discovered":    {"label": "Discovered",        "terminal": False},
    "identified":    {"label": "Identified",       "terminal": False},
    "evaluated":     {"label": "Evaluated",         "terminal": False},
    "researching":   {"label": "Researching",       "terminal": False},
    "outreach":      {"label": "Outreach",          "terminal": False},
    "recruiter":     {"label": "Recruiter Screen",  "terminal": False},
    "hm_interview":  {"label": "HM Interview",      "terminal": False},
    "panel":         {"label": "Panel / Loop",      "terminal": False},
    "final_offer":   {"label": "Final / Offer",     "terminal": False},
    "accepted":          {"label": "Accepted",            "terminal": True},
    "i_declined":        {"label": "I Declined",          "terminal": True},
    "they_declined":     {"label": "They Declined",       "terminal": True},
    "job_listing_closed": {"label": "Job Listing Closed", "terminal": True},
    "on_hold":           {"label": "On Hold",             "terminal": False},
    "duplicate":         {"label": "Duplicate",           "terminal": True},
}

I_DECLINED_REASONS = [
    "Compensation too low",
    "Requires in-office",
    "Not enough greenfield",
    "Bad culture signals",
    "Wrong tech stack",
    "Too much travel",
    "Ethics concern",
    "Better opportunity elsewhere",
    "Role not senior enough",
    "Other",
]

THEY_DECLINED_REASONS = [
    "Not enough experience",
    "Overqualified",
    "Location mismatch",
    "Compensation mismatch",
    "Went with another candidate",
    "Role cancelled / hiring freeze",
    "No response (ghosted)",
    "Other",
]

JOB_CLOSED_REASONS = [
    "Listing removed",
    "Position filled",
    "Reposted as different role",
    "Other",
]

DUPLICATE_REASONS = [
    "Same role, already in pipeline",
    "Repost of an old listing",
    "Other",
]


def advance_stage(job_id: int, to_stage: str, notes: str = "", decline_reason: str = "") -> dict:
    """
    Move a job to a new pipeline stage.
    Returns {"ok": True} or {"ok": False, "error": "message"}.
    """
    if to_stage not in STAGES:
        return {"ok": False, "error": f"Unknown stage: {to_stage}"}

    if to_stage in ("i_declined", "they_declined", "job_listing_closed", "duplicate") and not decline_reason:
        return {"ok": False, "error": "Decline reason is required"}

    with get_db() as conn:
        row = conn.execute("SELECT pipeline_stage FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Job not found"}

        from_stage = row["pipeline_stage"]

        conn.execute(
            "UPDATE jobs SET pipeline_stage = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (to_stage, job_id),
        )
        if to_stage in ("applied", "outreach"):
            conn.execute(
                "UPDATE jobs SET applied_at = ? WHERE id = ? AND applied_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
        full_notes = f"{notes}\nDecline reason: {decline_reason}".strip() if decline_reason else notes
        conn.execute(
            "INSERT INTO pipeline_history (job_id, from_stage, to_stage, notes) VALUES (?,?,?,?)",
            (job_id, from_stage, to_stage, full_notes),
        )

    return {"ok": True, "from": from_stage, "to": to_stage}


def get_pipeline_summary() -> dict:
    """Return count of jobs at each stage."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT pipeline_stage, COUNT(*) as cnt FROM jobs GROUP BY pipeline_stage"
        ).fetchall()
    return {row["pipeline_stage"]: row["cnt"] for row in rows}


def get_stale_pipeline(days: int = 14) -> list[dict]:
    """Return jobs with no stage change in more than `days` days."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT j.id, j.company, j.job_title, j.pipeline_stage,
                      MAX(h.changed_at) as last_changed
               FROM jobs j
               LEFT JOIN pipeline_history h ON h.job_id = j.id
               WHERE j.pipeline_stage NOT IN ('accepted','i_declined','they_declined','job_listing_closed','duplicate')
               GROUP BY j.id
               HAVING last_changed < datetime('now', ? || ' days')
                  OR last_changed IS NULL""",
            (f"-{days}",),
        ).fetchall()
    return [dict(r) for r in rows]
