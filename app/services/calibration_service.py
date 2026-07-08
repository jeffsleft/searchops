from app.models import get_db
from app.config import CALIBRATION_MIN_SAMPLE

VALID_OUTCOMES = {
    "applied", "phone_screen", "interview", "offer",
    "rejected_them", "rejected_me", "ghosted",
}


def record_outcome(job_id: int, outcome: str, notes: str | None = None, conn=None) -> None:
    """Insert an outcome row. Pass an existing `conn` to join the caller's transaction
    (required by record_stage_change — a second get_db() connection would deadlock
    against the caller's still-open write transaction to the same sqlite file)."""
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome '{outcome}'. Must be one of: {sorted(VALID_OUTCOMES)}")
    if conn is not None:
        conn.execute(
            "INSERT INTO application_outcomes (job_id, outcome, notes) VALUES (?, ?, ?)",
            (job_id, outcome, notes),
        )
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO application_outcomes (job_id, outcome, notes) VALUES (?, ?, ?)",
            (job_id, outcome, notes),
        )


def get_calibration_summary() -> dict:
    """
    Extended calibration summary with hit-rate bucketing.

    Returns per-bucket hit rates (High/Medium/Low interview probabilities) with n gates:
    - Buckets with n < 3 report rate as null (noise)
    - Global insufficient_data flag when total n_with_outcome < CALIBRATION_MIN_SAMPLE
    - No mean_brier (v1 mistake removed per spec)
    """
    from app.pipeline.calibration import compute_calibration

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                ao.outcome,
                COUNT(*) AS cnt,
                ROUND(AVG(j.final_score), 2) AS avg_score,
                ROUND(MIN(j.final_score), 2) AS min_score,
                ROUND(MAX(j.final_score), 2) AS max_score
            FROM application_outcomes ao
            JOIN jobs j ON j.id = ao.job_id
            GROUP BY ao.outcome
            ORDER BY cnt DESC
            """
        ).fetchall()

        n_with_outcome = conn.execute("SELECT COUNT(*) FROM application_outcomes").fetchone()[0]
        n_scored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE final_score IS NOT NULL AND auto_rejected = 0"
        ).fetchone()[0]

    # Compute per-bucket hit rates from existing calibration service
    cal = compute_calibration()
    by_probability = cal.get("by_probability", [])

    # Format hit_rate_by_bucket with n gates (buckets with n < 3 → null rate)
    hit_rate_by_bucket = []
    for bucket in by_probability:
        label = bucket["label"]
        n = bucket["total"]
        if n < 3:
            rate = None
        else:
            rate = round(bucket["rate"], 1) if bucket["rate"] else 0
        hit_rate_by_bucket.append({
            "label": label,
            "rate": rate,
            "n": n,
        })

    insufficient_data = n_with_outcome < CALIBRATION_MIN_SAMPLE

    return {
        "breakdown": [dict(r) for r in rows],
        "total_outcomes": n_with_outcome,
        "n_with_outcome": n_with_outcome,
        "n_scored": n_scored,
        "hit_rate_by_bucket": hit_rate_by_bucket,
        "insufficient_data": insufficient_data,
    }


def get_job_outcomes(job_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, outcome, notes, recorded_at FROM application_outcomes WHERE job_id = ? ORDER BY id DESC",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]
