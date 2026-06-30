from app.models import get_db

VALID_OUTCOMES = {
    "applied", "phone_screen", "interview", "offer",
    "rejected_them", "rejected_me", "ghosted",
}


def record_outcome(job_id: int, outcome: str, notes: str | None = None) -> None:
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome '{outcome}'. Must be one of: {sorted(VALID_OUTCOMES)}")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO application_outcomes (job_id, outcome, notes) VALUES (?, ?, ?)",
            (job_id, outcome, notes),
        )


def get_calibration_summary() -> dict:
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

        total = conn.execute("SELECT COUNT(*) FROM application_outcomes").fetchone()[0]
        scored_total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE final_score IS NOT NULL AND auto_rejected = 0"
        ).fetchone()[0]

    return {
        "breakdown": [dict(r) for r in rows],
        "total_outcomes": total,
        "total_scored": scored_total,
    }


def get_job_outcomes(job_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, outcome, notes, recorded_at FROM application_outcomes WHERE job_id = ? ORDER BY id DESC",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]
