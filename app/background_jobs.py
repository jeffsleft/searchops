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


def score_backlog(limit: int = 15) -> dict:
    """Score discovered roles the scan left unscored.

    The scan scores at most DISCOVERY_FULL_SCORE_CAP roles per run and skips URLs it
    already has, so anything past the cap was never scored. This runs every
    scheduler tick (6h), newest first, up to `limit` LLM-scored roles, paced for the
    free tier. Roles from the last 14 days only; older ones are stale.
    """
    import time
    from app.config import load_profile
    from app.discovery.hunter import _LLM_CALL_PACING_SECONDS, _auto_score_discovery

    # Skips roles found in the last 2 hours (the scan may be scoring them right now)
    # and roles that already failed 3 times (no JD, unfetchable), so the queue
    # can't get stuck on the same unscorable rows.
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, url, jd_text FROM jobs "
            "WHERE pipeline_stage IN ('discovered', 'identified') AND final_score IS NULL "
            "  AND auto_rejected = 0 AND url IS NOT NULL AND COALESCE(score_attempts, 0) < 3 "
            "  AND date_found >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-14 days') "
            "  AND date_found <= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-2 hours') "
            "ORDER BY date_found DESC, id DESC LIMIT ?", (limit * 3,)
        ).fetchall()
    profile, budget = load_profile(), {"remaining": limit}
    counts: dict = {}
    for r in rows:
        if budget["remaining"] <= 0:
            break
        status = _auto_score_discovery(r["id"], r["url"], profile, budget, feed_text=r["jd_text"] or "")
        counts[status] = counts.get(status, 0) + 1
        if status != "success":
            with get_db() as conn:
                conn.execute("UPDATE jobs SET score_attempts = COALESCE(score_attempts, 0) + 1 WHERE id = ?",
                             (r["id"],))
        if status == "success":
            time.sleep(_LLM_CALL_PACING_SECONDS)  # pace LLM calls only
    log_task_event("score_backlog", "completed", f"{counts}")
    return counts


def _board_key(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    first = p.path.strip("/").split("/", 1)[0].lower()
    return f"{p.netloc.lower()}/{first}"


def close_dead_listings(dry_run: bool = False) -> dict:
    """Close discovered roles whose posting is gone from the company's job board.

    Replaces hand sweeps like the 54 dead listings closed on 2026-09-20. Safety:
    - only roles the scan found (their URL is the board's own form), still
      discovered/identified, found 2+ days ago;
    - a board that can't be fully listed (None) is skipped, never read as "all closed";
    - only URLs whose host AND first path segment (the board: /posthog, /visa,
      /careers) match a current listing are judged. Lever/Ashby/Greenhouse share one
      host across companies, so host alone would let a renamed board mass-close;
    - each job's stage is re-read just before closing (listing can take minutes).
    """
    from collections import defaultdict
    from app.discovery.ats_clients import list_job_urls
    from app.pipeline.tracker import advance_stage

    with get_db() as conn:
        rows = conn.execute(
            "SELECT j.id, j.url, j.company, c.ats_type, c.ats_handle FROM jobs j "
            "JOIN companies c ON c.id = j.company_id "
            "WHERE j.discovery_source = 'hunter' AND j.pipeline_stage IN ('discovered', 'identified') "
            "  AND j.url IS NOT NULL AND c.ats_type IS NOT NULL AND c.ats_type != 'generic' "
            "  AND j.date_found <= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-2 days')"
        ).fetchall()
    by_board = defaultdict(list)
    for r in rows:
        by_board[(r["ats_type"], r["ats_handle"])].append(r)

    closed, skipped_boards = [], []
    for (ats_type, handle), jobs in by_board.items():
        live = list_job_urls(ats_type, handle)
        if not live:
            skipped_boards.append(handle)
            continue
        boards = {_board_key(u) for u in live}
        for r in jobs:
            if _board_key(r["url"]) not in boards or r["url"] in live:
                continue
            if not dry_run:
                with get_db() as conn:
                    stage = conn.execute("SELECT pipeline_stage FROM jobs WHERE id = ?", (r["id"],)).fetchone()
                if not stage or stage[0] not in ("discovered", "identified"):
                    continue  # moved on while boards were being listed
                advance_stage(r["id"], "job_listing_closed", decline_reason="Listing removed",
                              notes="auto: no longer on the company's job board")
            closed.append(f"{r['id']} {r['company']}")
    summary = {"dry_run": dry_run, "closed": len(closed), "checked": len(rows),
               "skipped_boards": len(skipped_boards), "jobs": closed}
    print(f"[close_dead_listings] {summary['closed']} of {len(rows)} closed; "
          f"{len(skipped_boards)} board(s) skipped (dry_run={dry_run})")
    for c in closed:
        print(f"[close_dead_listings]   {c}")
    if not dry_run:
        log_task_event("close_dead_listings", "completed",
                       f"closed {len(closed)} of {len(rows)}; skipped {len(skipped_boards)} boards")
    return summary


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
    "score_backlog": score_backlog,
    "close_dead_listings": close_dead_listings,
    "prune_observability": prune_observability,
    "weekly_digest": weekly_digest,
    "backup": backup,
    "progress_snapshot": progress_snapshot,
}
