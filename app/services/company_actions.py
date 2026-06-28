"""
Company-action business logic extracted from routes.py.

Owns the "do the work" half of company/discovery handlers: the one-time Tier A
import, the batch research sweep, and a single-company scan. Handlers parse the
request, call one of these, then render the result (outcome_charter KR1).
"""
import json
import logging
from datetime import date

from app.models import get_db
from app.services.discovery_service import (
    do_scan_company,
    get_company_scan_status,
    recompute_company_match_summary,
)


def import_tier_a_companies() -> dict:
    """Google Sheets removed — add companies via the Companies tab."""
    return {"status": "fetch_error", "error": "Google Sheets removed. Add companies via the Companies tab."}


def research_companies_batch(batch_research_fn=None, batch_size: int = 5) -> str:
    """Research the next batch of un-researched companies.

    Spawns the Modal background task when available; otherwise runs the research
    sequentially in-process. Returns a human-readable status message.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM companies WHERE research_date IS NULL ORDER BY date_added ASC LIMIT ?",
            (batch_size,),
        ).fetchall()

    if not rows:
        return "All companies already researched."

    ids = [r["id"] for r in rows]

    if batch_research_fn:
        batch_research_fn.spawn(ids)
        return f"Started background research for {len(ids)} companies in parallel. Refresh in 1-2 mins."

    from app.scoring.research import research_company, assess_company_fit
    done = 0
    for r_id in ids:
        try:
            with get_db() as conn:
                r = conn.execute("SELECT name, funding_stage FROM companies WHERE id = ?", (r_id,)).fetchone()
            research = research_company(r["name"])
            fit = assess_company_fit(r["name"], research)

            research["fit_rationale"] = fit.get("fit_rationale")
            research["fit_justification"] = fit.get("fit_justification")
            research["need_rationale"] = fit.get("need_rationale")
            research["need_justification"] = fit.get("need_justification")

            with get_db() as conn:
                conn.execute(
                    """UPDATE companies SET research_json=?, research_date=?,
                       fit_score=?, need_assessment=?, funding_stage=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (json.dumps(research), str(date.today()),
                     fit.get("fit_score"), fit.get("need_assessment"),
                     research.get("funding_stage", r["funding_stage"] or ""), r_id),
                )
            done += 1
        except Exception as e:
            logging.error("[research-batch] Failed: %s", e)
    return f"Researched {done} companies."


def scan_target_company(co_id: int) -> dict:
    """Run an on-demand ATS scan for one hunt-target company.

    Blocking (ATS fetch); call from a worker thread in async handlers. Returns:
      {"status": "not_found"}
      {"status": "error", "error": str}
      {"status": "ok", "scan": dict}   # scan = get_company_scan_status payload
    """
    with get_db() as conn:
        company = conn.execute(
            "SELECT id, name, ats_type, ats_handle, careers_url FROM companies WHERE id = ?", (co_id,)
        ).fetchone()
    if not company:
        return {"status": "not_found"}

    try:
        do_scan_company(dict(company))
    except Exception as e:
        logging.error("scan_target_company failed for company %s: %s", co_id, e)
        return {"status": "error", "error": str(e)}

    status = get_company_scan_status(co_id)
    if not status:
        return {"status": "not_found"}
    return {"status": "ok", "scan": status}


def refresh_company_matches(co_id: int) -> dict | None:
    """Refresh a company's matching-roles summary for the target lists.

    If an ATS is known (careers_url or ats_handle), re-scan it for open roles —
    do_scan_company inserts any new matches and recomputes the summary. Otherwise
    just recompute from existing discovered jobs. Blocking (ATS fetch); call from a
    worker thread. Returns the updated company row, or None if the company is gone.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, ats_type, ats_handle, careers_url FROM companies WHERE id = ?", (co_id,)
        ).fetchone()
    if not row:
        return None
    company = dict(row)

    if company.get("careers_url") or company.get("ats_handle"):
        try:
            do_scan_company(company)  # inserts new roles + recomputes summary
        except Exception as e:
            logging.error("refresh_company_matches scan failed for %s: %s", co_id, e)
            recompute_company_match_summary(co_id)
    else:
        recompute_company_match_summary(co_id)

    with get_db() as conn:
        updated = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
    return dict(updated) if updated else None
