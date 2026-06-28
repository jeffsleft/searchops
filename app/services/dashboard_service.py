"""
Dashboard data aggregation service extracted from routes.py.

Owns all dashboard-specific queries, stats calculations, and data transformations.
Returns a dict ready for template rendering.
"""
from datetime import datetime, timezone
from app.models import get_db
from app.pipeline.followups import get_followups_due
from app.pipeline.prep import get_upcoming_interviews


TERMINAL_STAGES = {
    'accepted', 'i_declined', 'they_declined', 'job_listing_closed',
    'duplicate', 'identified', 'evaluated', 'discovered'
}


def build_dashboard_data(archetype: str, _enrich_job_fn) -> dict:
    """
    Build all dashboard data: live jobs, high-score list, recent, pipeline, etc.

    Args:
        archetype: Selected role archetype filter (empty string = no filter)
        _enrich_job_fn: Callable to enrich a job dict (from routes._enrich_job)

    Returns:
        Dict with keys: live, high, recent, in_pipeline, interviewing, avg_score,
        outreach_queue, total_scored, discovered_count, followups_due, upcoming,
        last_scan_time, new_since_last
    """
    with get_db() as conn:
        # All live (non-auto-rejected, scored) jobs
        query = "SELECT * FROM jobs WHERE auto_rejected = 0 AND final_score IS NOT NULL"
        params = []
        if archetype:
            query += " AND role_archetype = ?"
            params.append(archetype)
        query += " ORDER BY final_score DESC"
        live_rows = conn.execute(query, params).fetchall()
        live_list = [_enrich_job_fn(dict(r)) for r in live_rows]

        # High-score jobs (≥ 7.0), top 6 — exclude terminal/triage stages
        high = [j for j in live_list
                if j.get('final_score', 0) >= 7.0
                and j.get('pipeline_stage') not in TERMINAL_STAGES][:6]

        # Recently added — sorted by date_found DESC, top 5 — exclude terminal/triage stages
        recent_pool = [j for j in live_list if j.get('pipeline_stage') not in TERMINAL_STAGES]
        recent = sorted(recent_pool, key=lambda x: x.get('date_found') or '', reverse=True)[:5]
        in_pipeline = [j for j in live_list if j.get('pipeline_stage') not in TERMINAL_STAGES]

        # Interviewing (advanced stages)
        interviewing_stages = {'recruiter', 'hm_interview', 'panel', 'final_offer'}
        interviewing = [j for j in live_list if j.get('pipeline_stage') in interviewing_stages]

        # Average score
        avg_score = (
            sum(j.get('final_score', 0) for j in live_list) / len(live_list)
            if live_list else 0.0
        )

        # Outreach queue — top 4 Watchlist companies by fit_score
        outreach_rows = conn.execute(
            "SELECT * FROM companies WHERE status = 'Watchlist' ORDER BY fit_score DESC LIMIT 4"
        ).fetchall()
        outreach_queue = [dict(r) for r in outreach_rows]

        # Total scored count
        total_scored = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE final_score IS NOT NULL"
        ).fetchone()["c"]

        # Newly discovered count
        discovered_count = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE pipeline_stage = 'discovered'"
        ).fetchone()["c"]

        followups_due = get_followups_due(conn)
        upcoming = get_upcoming_interviews(conn)

        # Last scan time from task_log
        scan_row = conn.execute(
            "SELECT logged_at FROM task_log WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if scan_row:
            try:
                dt = datetime.fromisoformat(scan_row["logged_at"].replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - dt
                if delta.total_seconds() < 60:
                    last_scan_time = "just now"
                elif delta.total_seconds() < 3600:
                    last_scan_time = f"{int(delta.total_seconds() // 60)}m ago"
                elif delta.total_seconds() < 86400:
                    last_scan_time = f"{int(delta.total_seconds() // 3600)}h ago"
                else:
                    last_scan_time = f"{int(delta.total_seconds() // 86400)}d ago"
            except Exception:
                last_scan_time = "recently"
        else:
            last_scan_time = "never"

        # New since last (jobs added in last 7 days)
        new_since_last = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE date_found >= date('now', '-7 days')"
        ).fetchone()["c"]

    return {
        'live': live_list,
        'high': high,
        'recent': recent,
        'in_pipeline': in_pipeline,
        'interviewing': interviewing,
        'avg_score': avg_score,
        'outreach_queue': outreach_queue,
        'total_scored': total_scored,
        'discovered_count': discovered_count,
        'followups_due': followups_due,
        'upcoming': upcoming,
        'last_scan_time': last_scan_time,
        'new_since_last': new_since_last,
    }
