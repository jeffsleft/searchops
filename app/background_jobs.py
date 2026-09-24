"""Jobs that run on the web container's BackgroundRunner.

Scheduled work used to run in its own Modal containers, each mounting the
Volume and able to overwrite the web container's writes on exit. Now the
scheduler only enqueues a job name (app/main.py) and the web container runs
the matching function below. See app/background.py for why.

JOBS maps a queue name to its function. A name is also the runner's dedupe
key, so "research" from the scheduler and from the Companies page can't run
twice at once.
"""
import logging

from app.models import get_db, log_task_event

log = logging.getLogger("app.background")


def discovery_scan() -> dict:
    """Run the discovery scan. A crash anywhere in the scan skips the scan's own
    completion log line, so record the failure here before re-raising."""
    from app.discovery import hunter  # attribute lookup at call time, so tests can patch it
    try:
        stats = hunter.run_discovery_scan()
    except Exception as e:
        log.error("[discovery] Crashed: %s", e)
        log_task_event("discovery_scan", "failed", f"Crashed: {str(e)[:200]}")
        raise
    log.info("[discovery] Done: %s", stats)
    return stats


def research_companies(company_ids: list[int]) -> int:
    """Research companies one after another. Returns how many succeeded."""
    from app.services.research_service import do_research_company

    n = len(company_ids)
    log_task_event("batch_research", "started", f"Queued {n} companies for research")
    done = 0
    for co_id in company_ids:
        with get_db() as conn:
            r = conn.execute("SELECT name, funding_stage FROM companies WHERE id = ?", (co_id,)).fetchone()
        if not r:
            continue
        name = r["name"]
        log_task_event("research", "started", f"Researching {name}", name)
        # do_research_company logs and swallows its own exceptions; check the bool.
        if do_research_company(co_id, dict(r)):
            done += 1
            log_task_event("research", "completed", f"Research done for {name}", name)
        else:
            log_task_event("research", "failed", "do_research_company failed — see server logs", name)
    status = "completed" if done == n else "partial"
    log_task_event("batch_research", status, f"Finished {done}/{n} successfully")
    return done


def research_next_batch(limit: int = 5) -> int:
    """Research the oldest not-yet-researched companies."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM companies WHERE research_date IS NULL ORDER BY date_added ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return research_companies([r["id"] for r in rows]) if rows else 0


def prune_observability() -> None:
    from app.config import USAGE_RETENTION_DAYS
    from app.models import prune_observability_tables
    prune_observability_tables(USAGE_RETENTION_DAYS)


def weekly_digest() -> bool:
    from app.notifications.slack import send_weekly_digest
    return send_weekly_digest()


def backup() -> None:
    from app.maintenance.db_backup import backup_database
    backup_database()


def progress_snapshot() -> dict:
    from app.crons.progress import snapshot_progress
    return snapshot_progress()


JOBS = {
    "discovery_scan": discovery_scan,
    "research": research_next_batch,
    "prune_observability": prune_observability,
    "weekly_digest": weekly_digest,
    "backup": backup,
    "progress_snapshot": progress_snapshot,
}
