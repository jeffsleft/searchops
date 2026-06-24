"""
Company discovery scan business logic.
Extracted from routes.py to keep route handlers thin.
"""
import json
import yaml
from datetime import datetime, timezone
from pathlib import Path

from app.models import get_db

_TITLE_FILTERS: dict | None = None

def _get_title_filters() -> dict:
    global _TITLE_FILTERS
    if _TITLE_FILTERS is None:
        config_path = Path(__file__).parent.parent / "discovery" / "hunt_targets.yaml"
        try:
            with open(config_path) as f:
                _TITLE_FILTERS = yaml.safe_load(f).get("title_filters", {})
        except Exception:
            _TITLE_FILTERS = {}
    return _TITLE_FILTERS


# Pipeline stages that mean a role is no longer an open, matchable opportunity.
_CLOSED_STAGES = ("job_listing_closed", "they_declined", "i_declined", "accepted")


def recompute_company_match_summary(co_id: int) -> dict:
    """Recompute a company's match summary (count of matching open roles + best score)
    from its discovered jobs, and persist it onto the company row.

    A "matching open role" is a hunt-discovered job for this company that hasn't
    reached a terminal stage. Best score prefers the full final_score, falling back
    to the lightweight discovery score. Returns the persisted summary.
    """
    placeholders = ",".join("?" * len(_CLOSED_STAGES))
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        row = db.execute(
            f"""SELECT COUNT(*) AS cnt,
                       MAX(COALESCE(final_score, lightweight_score)) AS best
                  FROM jobs
                 WHERE company_id = ?
                   AND COALESCE(discovery_source, '') LIKE 'hunt%'
                   AND COALESCE(pipeline_stage, 'discovered') NOT IN ({placeholders})""",
            (co_id, *_CLOSED_STAGES),
        ).fetchone()
        cnt = (row["cnt"] if row else 0) or 0
        best = row["best"] if row else None
        db.execute(
            "UPDATE companies SET match_count=?, match_best_score=?, matches_refreshed_at=? WHERE id=?",
            (cnt, best, now, co_id),
        )
    return {"match_count": cnt, "match_best_score": best, "matches_refreshed_at": now}


def get_company_scan_status(co_id: int) -> dict | None:
    """Query a company's scan status by ID.

    Returns dict with keys: id, name, last_scanned, scan_error, hunt_enabled, ats_type, ats_handle
    or None if not found.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT id, name, last_scanned, scan_error, hunt_enabled, ats_type, ats_handle FROM companies WHERE id = ?",
            (co_id,)
        ).fetchone()
    return dict(row) if row else None


def do_scan_company(company: dict) -> None:
    """Scan a company's ATS for new openings and insert discovered jobs."""
    from app.discovery.ats_clients import fetch_jobs_for_company
    from app.discovery.matcher import passes_title_filter, generate_fit_analysis

    co_id = company['id']
    ats_type = company['ats_type'] or 'unknown'
    ats_handle = company['ats_handle'] or ''
    careers_url = company['careers_url'] or ''

    try:
        raw_jobs = fetch_jobs_for_company(ats_type, ats_handle, careers_url)

        for job in raw_jobs:
            title = job.get('title', '')
            url = job.get('url', '')
            if not title or not url:
                continue
            if not passes_title_filter(title, _get_title_filters()):
                continue
            with get_db() as db:
                existing = db.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
            if existing:
                continue
            fit = generate_fit_analysis(title, job.get('description', ''), company['name'])
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as db:
                db.execute("""
                    INSERT INTO jobs (
                        company_id, company, job_title, url, pipeline_stage, discovery_source,
                        fit_bullets, lightweight_score, date_found, date_added
                    ) VALUES (?, ?, ?, ?, 'discovered', 'hunter', ?, ?, ?, ?)
                """, (
                    co_id, company['name'], title, url,
                    json.dumps(fit.get('fit_bullets', [])),
                    fit.get('preliminary_score', 5.0),
                    now, now
                ))

        with get_db() as db:
            db.execute(
                "UPDATE companies SET last_scanned=?, scan_error=NULL WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), co_id)
            )

    except Exception as e:
        with get_db() as db:
            db.execute("UPDATE companies SET scan_error=? WHERE id=?", (str(e), co_id))

    # Always refresh the match summary — even on a no-new-roles scan, this keeps the
    # count + best score current (and populates it for companies scanned before WP-L).
    try:
        recompute_company_match_summary(co_id)
    except Exception as e:
        import logging
        logging.warning("recompute_company_match_summary failed for %s: %s", co_id, e)
