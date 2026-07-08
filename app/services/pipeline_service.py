"""
Pipeline view data aggregation service extracted from routes.py.

Owns pipeline queries, stage grouping, stale calculations, and job aggregations.
Returns a dict ready for template rendering.
"""
import sqlite3
from app.models import get_db
from app.pipeline.tracker import get_stale_pipeline


TERMINAL_STAGES = {'accepted', 'i_declined', 'they_declined', 'job_listing_closed', 'duplicate'}


def build_pipeline_view_data(archetype: str, _enrich_job_fn) -> dict:
    """
    Build pipeline view data: jobs grouped by stage, stale items, counts.

    Args:
        archetype: Selected role archetype filter (empty string = no filter)
        _enrich_job_fn: Callable to enrich a job dict (from routes._enrich_job)

    Returns:
        Dict with keys: jobs_by_stage, stale_items, total, active, max_stage_count
    """
    with get_db() as conn:
        query = "SELECT * FROM jobs WHERE auto_rejected = 0"
        params = []
        if archetype:
            query += " AND role_archetype = ?"
            params.append(archetype)
        query += " ORDER BY final_score DESC"
        rows = conn.execute(query, params).fetchall()

    jobs_by_stage: dict = {}
    for r in rows:
        j = _enrich_job_fn(dict(r))
        stage = j.get("pipeline_stage") or "identified"
        jobs_by_stage.setdefault(stage, []).append(j)

    stale_items = get_stale_pipeline()
    total = len(rows)
    active = sum(
        len(v) for k, v in jobs_by_stage.items()
        if k not in TERMINAL_STAGES
    )
    max_stage_count = max((len(v) for v in jobs_by_stage.values()), default=1)

    return {
        'jobs_by_stage': jobs_by_stage,
        'stale_items': stale_items,
        'total': total,
        'active': active,
        'max_stage_count': max_stage_count,
    }


def record_stage_change(
    conn: sqlite3.Connection,
    job_id: int,
    to_stage: str,
    note: str = None,
    changed_by: str = "user"
) -> None:
    """
    THE ONLY SANCTIONED WAY TO CHANGE pipeline_stage.

    Atomically updates jobs.pipeline_stage and writes exactly one pipeline_history row
    with the from_stage captured before the update. If the to_stage is terminal
    (applied/phone_screen/interview/offer/rejected_them/rejected_me/ghosted),
    also records the outcome via calibration_service.

    Args:
        conn: sqlite3 connection
        job_id: job ID to transition
        to_stage: new pipeline_stage value
        note: optional history note
        changed_by: who made the change (default "user")
    """
    # Map pipeline stages to outcome types (for terminal transitions)
    STAGE_TO_OUTCOME = {
        'applied': 'applied',
        'outreach': 'applied',
        'recruiter': 'phone_screen',
        'hm_interview': 'interview',
        'panel': 'interview',
        'final_offer': 'interview',
        'offer': 'offer',
        'accepted': 'offer',
        'i_declined': 'rejected_them',
        'they_declined': 'rejected_me',
    }

    # Read current stage before update
    row = conn.execute("SELECT pipeline_stage FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise ValueError(f"Job {job_id} not found")

    from_stage = row["pipeline_stage"]

    # Update the job's pipeline_stage
    conn.execute(
        "UPDATE jobs SET pipeline_stage = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (to_stage, job_id),
    )

    # Write exactly one pipeline_history row with from_stage
    conn.execute(
        "INSERT INTO pipeline_history (job_id, from_stage, to_stage, notes, changed_by) VALUES (?,?,?,?,?)",
        (job_id, from_stage, to_stage, note or "", changed_by),
    )

    # If this is a terminal transition, record the outcome
    if to_stage in STAGE_TO_OUTCOME:
        outcome = STAGE_TO_OUTCOME[to_stage]
        try:
            from app.services.calibration_service import record_outcome
            # Must reuse `conn` here, not open a second get_db() connection: a nested
            # connection would deadlock against this still-open write transaction on
            # the same sqlite file (confirmed empirically — 5s timeout then
            # "database is locked", silently swallowed by this except).
            record_outcome(job_id, outcome, note, conn=conn)
        except Exception:
            # If outcome recording fails, don't fail the stage change
            pass
