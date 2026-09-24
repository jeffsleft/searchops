"""
Pipeline stage management. Enforces allowed transitions, logs history,
requires decline reasons on terminal stages.
"""
from app.models import get_db

# The one list of pipeline stages. Every screen reads labels, order and
# descriptions from here (Jinja global STAGES, app/routes.py); never hardcode a
# stage label or a set of stage codes anywhere else. Order is display order.
STAGES = {
    "discovered":    {"label": "Discovered",       "terminal": False, "desc": "New role waiting for your review"},
    "identified":    {"label": "Identified",       "terminal": False, "desc": "Added by you; not fetched and scored yet"},
    "evaluated":     {"label": "Evaluated",        "terminal": False, "desc": "Reviewed and worth pursuing"},
    "researching":   {"label": "Researching",      "terminal": False, "desc": "Digging into the company"},
    "outreach":      {"label": "Outreach",         "terminal": False, "desc": "Reached out to a recruiter or hiring manager"},
    "applied":       {"label": "Applied",          "terminal": False, "desc": "Application submitted"},
    "recruiter":     {"label": "Recruiter Screen", "terminal": False, "desc": "Recruiter screen scheduled or done"},
    "hm_interview":  {"label": "HM Interview",     "terminal": False, "desc": "Interviewing with the hiring manager"},
    "panel":         {"label": "Panel / Loop",     "terminal": False, "desc": "Panel or full interview loop"},
    "final_offer":   {"label": "Final / Offer",    "terminal": False, "desc": "Final round or offer"},
    "accepted":           {"label": "Accepted",           "terminal": True,  "desc": "Offer accepted"},
    "i_declined":         {"label": "I Declined",         "terminal": True,  "desc": "You walked away after a real look"},
    "they_declined":      {"label": "They Declined",      "terminal": True,  "desc": "They passed on you"},
    "job_listing_closed": {"label": "Job Listing Closed", "terminal": True,  "desc": "The listing came down"},
    "on_hold":            {"label": "On Hold",            "terminal": False, "desc": "Paused on either side"},
    "duplicate":          {"label": "Duplicate",          "terminal": True,  "desc": "Same role tracked twice"},
    # One-click pass from the Discovered inbox. Kept apart from i_declined so a
    # two-second triage never counts as a considered decline in the metrics.
    "dismissed":          {"label": "Dismissed",          "terminal": True,  "desc": "Passed on from the Discovered inbox"},
}

TERMINAL_STAGES = frozenset(code for code, s in STAGES.items() if s["terminal"])

# Terminal stages that need a reason from the user (the decline dialog).
REASON_REQUIRED_STAGES = ("i_declined", "they_declined", "job_listing_closed", "duplicate")


def stage_label(code: str | None) -> str:
    """Display label for a stage code. Unknown codes show as-is, empty as a dash."""
    return STAGES[code]["label"] if code in STAGES else (code or "—")


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
    from app.services.pipeline_service import record_stage_change

    if to_stage not in STAGES:
        return {"ok": False, "error": f"Unknown stage: {to_stage}"}

    if to_stage in REASON_REQUIRED_STAGES and not decline_reason:
        return {"ok": False, "error": "Decline reason is required"}

    with get_db() as conn:
        row = conn.execute("SELECT pipeline_stage FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Job not found"}

        from_stage = row["pipeline_stage"]

        # Route through the single sanctioned writer. record_stage_change owns the
        # applied_at stamp and calibration-outcome logging for application transitions.
        full_notes = f"{notes}\nDecline reason: {decline_reason}".strip() if decline_reason else notes
        record_stage_change(conn, job_id, to_stage, note=full_notes, changed_by="user")

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
    terminal = sorted(TERMINAL_STAGES)
    placeholders = ",".join("?" * len(terminal))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT j.id, j.company, j.job_title, j.pipeline_stage,
                      MAX(h.changed_at) as last_changed
               FROM jobs j
               LEFT JOIN pipeline_history h ON h.job_id = j.id
               WHERE j.pipeline_stage NOT IN ({placeholders})
               GROUP BY j.id
               HAVING last_changed < datetime('now', ? || ' days')
                  OR last_changed IS NULL""",
            (*terminal, f"-{days}"),
        ).fetchall()
    return [dict(r) for r in rows]
