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


# Moving a job to one of these stages means an application went out — stamp applied_at
# (once, never cleared). Centralized here so every caller of the single sanctioned
# writer benefits (the detail-panel path update_job_stage never stamped it otherwise,
# which is why applied_at — and KR1 — undercounted).
APPLICATION_STAGES = ('applied', 'outreach')

# Map pipeline stages to calibration outcome types. Outcomes are only logged for jobs
# that have actually been applied to (applied_at IS NOT NULL) — a pre-application
# decline (e.g. i_declined straight out of 'identified') is funnel triage, not a
# calibration data point, so it must not create an outcome row. See W1-B.
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
# Note: 'job_listing_closed' is intentionally unmapped — a closed listing is an
# administrative state, not a candidate-facing result the scoring engine can be
# calibrated against. Logging it would inject an ambiguous signal into calibration.


def record_stage_change(
    conn: sqlite3.Connection,
    job_id: int,
    to_stage: str,
    note: str = None,
    changed_by: str = "user"
) -> None:
    """
    THE ONLY SANCTIONED WAY TO CHANGE pipeline_stage.

    Atomically, within the caller's transaction:
      1. Updates jobs.pipeline_stage and writes exactly one pipeline_history row with
         the from_stage captured before the update.
      2. Stamps jobs.applied_at (once) when moving to an application stage
         (applied / outreach) — this is the single place applied_at is set on a
         stage change, so KR1 counts every application regardless of which UI path
         made the move.
      3. Auto-logs a calibration outcome (application_outcomes) IFF the job has been
         applied to (applied_at set, including the stamp from step 2) AND the target
         stage maps to an outcome. Idempotent: never a second row for the same
         (job_id, outcome).

    Args:
        conn: sqlite3 connection
        job_id: job ID to transition
        to_stage: new pipeline_stage value
        note: optional history note
        changed_by: who made the change (default "user")
    """
    # Read current stage + applied_at before the update.
    row = conn.execute(
        "SELECT pipeline_stage, applied_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Job {job_id} not found")

    from_stage = row["pipeline_stage"]
    applied_at = row["applied_at"]

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

    # Stamp applied_at on the application transition (once). Do this BEFORE the outcome
    # gate so the very transition that marks the job "applied" also logs its 'applied'
    # outcome.
    if to_stage in APPLICATION_STAGES and applied_at is None:
        from datetime import datetime, timezone
        applied_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET applied_at = ? WHERE id = ? AND applied_at IS NULL",
            (applied_at, job_id),
        )

    # Auto-log a calibration outcome only for applied jobs.
    if to_stage in STAGE_TO_OUTCOME and applied_at is not None:
        outcome = STAGE_TO_OUTCOME[to_stage]
        try:
            # Idempotent: skip if this job already has a row for this outcome.
            existing = conn.execute(
                "SELECT 1 FROM application_outcomes WHERE job_id = ? AND outcome = ? LIMIT 1",
                (job_id, outcome),
            ).fetchone()
            if not existing:
                from app.services.calibration_service import record_outcome
                # Must reuse `conn` here, not open a second get_db() connection: a nested
                # connection would deadlock against this still-open write transaction on
                # the same sqlite file (confirmed empirically — 5s timeout then
                # "database is locked", silently swallowed by this except).
                record_outcome(job_id, outcome, note, conn=conn)
        except Exception:
            # If outcome recording fails, don't fail the stage change
            pass
