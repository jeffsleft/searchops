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

# Tier A import pulls from a fixed source sheet (distinct from the SSOT job sheet).
TIER_A_SHEET_ID = "1iyOD64_xfalt35JqHGaq-h1TzkZOcVwBoG3cE09bMYc"


def _fetch_tier_a_rows() -> list[dict]:
    """Read the 'Tier A' tab of the source sheet into a list of header->value dicts."""
    import requests as _req
    from urllib.parse import quote as _quote

    try:
        from app.sheets.sync import _get_creds as _get_credentials
    except ImportError:
        _get_credentials = None

    if _get_credentials:
        creds = _get_credentials()
    else:
        import os
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        token_content = os.environ.get("TOKEN_JSON_CONTENT", "")
        if not token_content:
            raise RuntimeError("TOKEN_JSON_CONTENT not set")
        info = json.loads(token_content)
        creds = Credentials.from_authorized_user_info(info, ["https://www.googleapis.com/auth/spreadsheets"])
        if creds.expired and creds.refresh_token:
            creds.refresh(GRequest())

    encoded = _quote("Tier A!A1:G60")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{TIER_A_SHEET_ID}/values/{encoded}"
    resp = _req.get(url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=15)
    resp.raise_for_status()
    rows = resp.json().get("values", [])
    if not rows:
        return []
    headers = rows[0]
    return [dict(zip(headers, row)) for row in rows[1:] if row]


def import_tier_a_companies() -> dict:
    """Upsert Tier A companies from the source sheet into the companies table.

    Safe to call repeatedly — upserts by name. Returns one of:
      {"status": "fetch_error", "error": str}
      {"status": "db_error", "error": str}
      {"status": "ok", "inserted": int, "updated": int}
    """
    try:
        rows = _fetch_tier_a_rows()
    except Exception as e:
        logging.error("import_tier_a_companies: sheet fetch failed: %s", e)
        return {"status": "fetch_error", "error": str(e)}

    today = str(date.today())
    inserted = 0
    updated = 0
    try:
        with get_db() as conn:
            for row in rows:
                name = (row.get("Company") or "").strip()
                if not name:
                    continue
                industry_category = (row.get("Category") or "").strip()
                why_interesting    = (row.get("What they do") or "").strip()
                headcount_estimate = (row.get("Headcount") or "").strip()
                funding_stage      = (row.get("Stage / Funding") or "").strip()
                remote_friendly    = (row.get("Remote?") or "").strip()
                nearest_hq         = (row.get("Nearest HQ to Auburn CA") or "").strip()

                existing = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE companies SET
                           tier_a = 1,
                           industry_category = ?,
                           why_interesting = COALESCE(why_interesting, ?),
                           headcount_estimate = ?,
                           funding_stage = ?,
                           remote_friendly = ?,
                           nearest_hq = ?,
                           updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (industry_category, why_interesting, headcount_estimate,
                         funding_stage, remote_friendly, nearest_hq, existing["id"]),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """INSERT INTO companies
                           (name, sector, why_interesting, headcount_estimate, funding_stage,
                            industry_category, remote_friendly, nearest_hq,
                            tier_a, date_added, source, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'tier_a_import', 'Watchlist')""",
                        (name, industry_category, why_interesting, headcount_estimate,
                         funding_stage, industry_category, remote_friendly, nearest_hq, today),
                    )
                    inserted += 1
    except Exception as e:
        logging.error("import_tier_a_companies: DB write failed: %s", e)
        return {"status": "db_error", "error": str(e)}

    return {"status": "ok", "inserted": inserted, "updated": updated}


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
