from datetime import datetime, timedelta, timezone

DUE_THRESHOLD_DAYS = 7
RESET_AFTER_LOG_DAYS = 5
ESCALATE_AT_COUNT = 3


def get_followups_due(conn) -> list[dict]:
    """
    Returns jobs in Applied/Outreach stage where a follow-up nudge is due:
      - applied_at is more than DUE_THRESHOLD_DAYS ago, AND
      - no follow-up logged in the last RESET_AFTER_LOG_DAYS, AND
      - snooze has expired or was never set.

    Each item carries: days_since, followup_count, last_followup_at, escalated.
    Sorted: escalated first, then oldest first.
    """
    now = datetime.now(timezone.utc)
    due_before = (now - timedelta(days=DUE_THRESHOLD_DAYS)).isoformat()

    rows = conn.execute("""
        SELECT
          j.id, j.company, j.job_title, j.pipeline_stage, j.applied_at,
          j.followup_snooze_until,
          COUNT(f.id)     AS followup_count,
          MAX(f.sent_at)  AS last_followup_at
        FROM jobs j
        LEFT JOIN followups f ON f.job_id = j.id
        WHERE j.pipeline_stage IN ('applied', 'outreach')
          AND j.applied_at IS NOT NULL
          AND j.applied_at <= ?
          AND (j.followup_snooze_until IS NULL OR j.followup_snooze_until <= ?)
        GROUP BY j.id
    """, (due_before, now.isoformat())).fetchall()

    out = []
    for r in rows:
        applied_at = datetime.fromisoformat(r["applied_at"])
        days_since = (now - applied_at).days
        last = r["last_followup_at"]
        if last:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).days < RESET_AFTER_LOG_DAYS:
                continue
        out.append({
            "id":              r["id"],
            "company":         r["company"],
            "job_title":       r["job_title"],
            "stage":           r["pipeline_stage"],
            "days_since":      days_since,
            "followup_count":  r["followup_count"],
            "last_followup_at": last,
            "escalated":       r["followup_count"] >= ESCALATE_AT_COUNT,
            "logo":            r["company"][:2].upper(),
        })

    out.sort(key=lambda x: (not x["escalated"], -x["days_since"]))
    return out
