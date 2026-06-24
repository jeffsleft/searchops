"""
Google Sheets sync layer. Reads "To Evaluate" tab for new URLs,
writes scores back. Never deletes rows.
"""
import json
import os
import time
import urllib.parse
import logging
from datetime import date

import requests

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials

from app.config import GOOGLE_SHEET_ID
from app.models import get_db
from app.providers.gemini import RateLimitedError
from app.scoring.research import score_job, research_company
from app.security.url_guard import validate_url

# AI_RULES.md §1: 5s delay between sequential LLM calls in batch loops
LLM_PACING_SECONDS = 5
MAX_JD_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB
_MAX_JSON_CHARS = 100_000


def _cap_json(data, label: str = "") -> str:
    """Serialize to JSON with a size cap — prevents oversized LLM output from bloating the DB.

    For list fields: trims items from the tail until the serialized form fits. If a single
    item alone exceeds the cap, returns [] with an error log rather than truncating mid-JSON.
    For non-list fields: logs a warning and returns the full (uncapped) string — truncating
    mid-JSON produces an unparseable column that breaks all downstream json.loads calls.
    """
    s = json.dumps(data)
    if len(s) <= _MAX_JSON_CHARS:
        return s
    logging.warning("[save_job] LLM field '%s' is %d chars (limit %d) — capping", label, len(s), _MAX_JSON_CHARS)
    if isinstance(data, list):
        trimmed = data[:]  # work on a copy — never mutate the caller's list
        while trimmed and len(json.dumps(trimmed)) > _MAX_JSON_CHARS:
            trimmed = trimmed[:-1]
        if not trimmed:
            logging.error(
                "[save_job] LLM field '%s' has a single item exceeding %d chars — "
                "storing empty list to preserve valid JSON",
                label, _MAX_JSON_CHARS,
            )
        return json.dumps(trimmed)
    # Non-list: truncating mid-JSON produces an unparseable column; return uncapped instead.
    logging.warning(
        "[save_job] LLM field '%s' is non-list (%s) and oversized — storing uncapped to preserve valid JSON",
        label, type(data).__name__,
    )
    return s

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",  # DB backup uploads + Docs sync
]
TO_EVALUATE_TAB = "To Evaluate"
RECRUITERS_TAB = "Recruiters"

RECRUITERS_HEADERS = [
    "Date Added", "Name", "Firm", "Specialty",
    "Email", "LinkedIn", "Relationship", "Notes",
]

# Column mapping — matches the actual sheet created by setup_sheet.py
COL = {
    "date_added": "A",
    "company": "B",
    "url": "C",            # Job URL — READ ONLY, never overwritten
    "status": "D",
    "final_score": "E",
    "pros": "F",
    "cons": "G",
    "greenfield": "H",
    "pricing_model": "I",
    "ceo_type": "J",
    "sector": "K",
    "funding_stage": "L",
    "total_raised": "M",
    "headcount": "N",
    "hq": "O",
    "tech_stack": "P",
    "competitors": "Q",
    "red_flags": "R",
    "outreach_hook": "S",
    "linkedin_url": "T",   # LinkedIn/secondary URL — READ ONLY, fallback fetch source
    "notes": "U",          # System notes — prepend only, never wiped
}


def _get_creds() -> Credentials:
    token_content = os.environ.get("TOKEN_JSON_CONTENT", "")
    if not token_content:
        token_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "token.json"))
        if os.path.exists(token_path):
            token_content = open(token_path).read()
    if not token_content:
        raise RuntimeError("No Google credentials found. Set TOKEN_JSON_CONTENT or provide token.json.")
    try:
        info = json.loads(token_content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"TOKEN_JSON_CONTENT is not valid JSON (offset {e.pos})") from None
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
        except Exception as e:
            raise RuntimeError(f"OAuth token refresh failed: {type(e).__name__}") from None
    return creds


def _sheets_get(creds: Credentials, range_str: str) -> dict:
    """GET a range via requests — handles SSL and URL-encodes tab names correctly."""
    encoded = urllib.parse.quote(range_str, safe="!:'")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{encoded}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {creds.token}"})
    resp.raise_for_status()
    return resp.json()


def _sheets_put(creds: Credentials, range_str: str, values: list) -> None:
    """PUT (update) a range via requests — handles SSL and URL-encodes tab names correctly."""
    encoded = urllib.parse.quote(range_str, safe="!:'")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{encoded}"
        f"?valueInputOption=RAW"
    )
    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {creds.token}"},
        json={"values": values},
    )
    resp.raise_for_status()


def _sheets_append(creds: Credentials, range_str: str, values: list) -> None:
    """Append rows after existing data."""
    encoded = urllib.parse.quote(range_str, safe="!:'")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{encoded}:append"
        f"?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    )
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {creds.token}"},
        json={"values": values},
    )
    resp.raise_for_status()


def _ensure_recruiters_tab(creds: Credentials) -> None:
    """Create the Recruiters tab with headers if it doesn't exist."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}:batchUpdate"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {creds.token}"},
            json={"requests": [{"addSheet": {"properties": {"title": RECRUITERS_TAB}}}]},
        )
        if resp.status_code == 200:
            _sheets_put(creds, f"'{RECRUITERS_TAB}'!A1:H1", [RECRUITERS_HEADERS])
    except Exception:
        pass  # Tab already exists — headers already set


def write_recruiter_to_sheet(recruiter: dict) -> None:
    """Append a recruiter row to the Recruiters tab."""
    from app.config import is_google_configured
    if not is_google_configured():
        logging.debug("Google Sheets not configured — skipping recruiter sheet sync")
        return
    try:
        creds = _get_creds()
        _ensure_recruiters_tab(creds)
        _sheets_append(creds, f"'{RECRUITERS_TAB}'!A:H", [[
            str(date.today()),
            recruiter.get("name", ""),
            recruiter.get("firm", ""),
            recruiter.get("specialty", ""),
            recruiter.get("email", ""),
            recruiter.get("linkedin_url", ""),
            recruiter.get("relationship_status", "Cold"),
            recruiter.get("notes", ""),
        ]])
    except Exception as e:
        print(f"Recruiter sheet sync failed (non-fatal): {e}")


def write_sheet_headers() -> None:
    """Write (or overwrite) column headers in row 1 of the To Evaluate tab."""
    from app.config import is_google_configured
    if not is_google_configured():
        logging.warning("Google Sheets not configured — cannot write headers")
        return
    creds = _get_creds()
    headers = [[
        "Date Added", "Company", "Job URL", "Status", "Final Score",
        "Pros", "Cons", "Greenfield Flag", "Pricing Model", "Founder Type",
        "Sector", "Funding Stage", "Total Raised", "Headcount", "HQ Location",
        "Tech Stack", "Competitors", "Red Flags", "Outreach Hook", "LinkedIn URL",
        "Notes",
    ]]
    _sheets_put(creds, f"'{TO_EVALUATE_TAB}'!A1:U1", headers)


def _prepend_note(creds, row_index: int, new_note: str, existing_note: str = "") -> None:
    """Prepend a dated note to column U, preserving any existing content."""
    dated = f"[{date.today()}] {new_note}"
    combined = dated if not existing_note.strip() else f"{dated}\n{existing_note.strip()}"
    _sheets_put(creds, f"'{TO_EVALUATE_TAB}'!U{row_index}:U{row_index}", [[combined]])


def read_to_evaluate() -> list[dict]:
    """Read all rows from To Evaluate tab. Returns list of row dicts."""
    creds = _get_creds()
    result = _sheets_get(creds, f"'{TO_EVALUATE_TAB}'!A:U")
    rows = result.get("values", [])
    if len(rows) < 2:
        return []
    headers = rows[0]
    return [
        {"row_index": i + 2, **dict(zip(headers, row))}
        for i, row in enumerate(rows[1:])
    ]


def write_score_to_row(row_index: int, score_record: dict) -> None:
    """Write scoring results back to a specific row. Column D (url) is never touched."""
    creds = _get_creds()
    research = score_record.get("_research", {})

    # A:B — date, company (skip C = Job URL, preserve it)
    _sheets_put(creds, f"'{TO_EVALUATE_TAB}'!A{row_index}:B{row_index}", [[
        str(date.today()),
        score_record.get("company", ""),
    ]])

    # D:S — status onward (C/Job URL intentionally skipped)
    _sheets_put(creds, f"'{TO_EVALUATE_TAB}'!D{row_index}:S{row_index}", [[
        "Scored",
        str(score_record.get("final_score", "")),
        score_record.get("pros", ""),
        score_record.get("cons", ""),
        score_record.get("greenfield", ""),
        score_record.get("pricing_model", ""),
        research.get("ceo_founder_type", ""),
        score_record.get("sector", ""),
        research.get("funding_stage", ""),
        research.get("total_raised", ""),
        research.get("headcount", ""),
        research.get("hq_location", ""),
        research.get("tech_stack", ""),
        research.get("competitors", ""),
        research.get("red_flags", ""),
        research.get("outreach_hook", ""),
    ]])


def save_stub_job_to_db(url: str, company: str = "") -> int:
    """Save a minimal stub record for a URL that failed JD fetch or parse.
    Prevents URL loss so future 'is job still open?' polling can work.
    pipeline_stage='identified' + final_score=NULL marks it as a stub.
    """
    from app.models import get_db, normalize_url
    today = str(date.today())
    norm_url = normalize_url(url)

    # Derive provisional company from ATS URL slug if the sheet column was blank
    resolved_company = company or extract_provisional_company(url) or "Unknown"
    # Flag LinkedIn stubs so the UI can surface paste-JD instead of wasting retries
    is_linkedin = is_linkedin_job_url(url)

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE source_url = ? OR (source_url IS NULL AND url = ?)",
            (norm_url, url),
        ).fetchone()
        if existing:
            return existing["id"]

        conn.execute(
            """INSERT INTO jobs
               (company, url, source_url, date_found, pipeline_stage, status, discovery_source,
                jd_fetch_attempts)
               VALUES (?,?,?,?,?,?,?,?)""",
            (resolved_company, url, norm_url, today, "identified", "Identified", "manual",
             3 if is_linkedin else 0),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return job_id


def save_job_to_db(url: str, score_record: dict) -> int:
    """Upsert job record into SQLite. Returns job_id."""
    from app.models import get_db, normalize_url
    today = str(date.today())
    norm_url = normalize_url(url)
    tech = score_record.get("tech_stack_detected", {})
    
    evidence = score_record.get("evidence", [])
    if not isinstance(evidence, list):
        logging.warning(f"Expected list for 'evidence', got {type(evidence)}. Coercing to [].")
        evidence = []
        
    mismatches = score_record.get("mismatches", [])
    if not isinstance(mismatches, list):
        logging.warning(f"Expected list for 'mismatches', got {type(mismatches)}. Coercing to [].")
        mismatches = []

    match_evidence = _cap_json(evidence, "match_evidence")
    match_mismatches = _cap_json(mismatches, "match_mismatches")
    match_bullets = _cap_json(score_record.get("tailored_bullets", []), "match_bullets")
    match_hooks = _cap_json(score_record.get("cover_letter_hooks", []), "match_hooks")
    differentiators = _cap_json(score_record.get("differentiator_themes", []), "differentiators")
    sections_to_drop = _cap_json(score_record.get("sections_to_drop", []), "sections_to_drop")
    role_archetype = score_record.get("role_archetype", "Other")
    
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE source_url = ? OR (source_url IS NULL AND url = ?)",
            (norm_url, url),
        ).fetchone()
        if existing:
            job_id = existing["id"]
            conn.execute(
                """UPDATE jobs SET
                   company=CASE WHEN COALESCE(company,'') IN ('','Unknown') AND ? != '' THEN ? ELSE company END,
                   job_title=CASE WHEN COALESCE(job_title,'') = '' AND ? != '' THEN ? ELSE job_title END,
                   final_score=?, deterministic_score=?, llm_adjustment=?,
                   auto_rejected=?, reject_reason=?, pros=?, cons=?,
                   greenfield=?, greenfield_rationale=?, pricing_model=?,
                   sector=?, recommended_angle=?, tech_stack_json=?,
                   flags_json=?, salary_range_detected=?, has_fde_model=?,
                   match_score=?, match_summary=?, match_evidence_json=?,
                   match_mismatches_json=?, match_bullets_json=?, match_hooks_json=?,
                   match_tailored_summary=?,
                   differentiator_themes_json=?, adjustment_weights_score=?,
                   posting_age_days=?, posting_date_raw=?, source_url=?, role_archetype=?,
                   interview_probability=?, interview_probability_rationale=?,
                   match_sections_to_drop_json=?,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    score_record.get("company") or "", score_record.get("company") or "",
                    score_record.get("job_title") or "", score_record.get("job_title") or "",
                    score_record.get("final_score"), score_record.get("deterministic_score"),
                    score_record.get("llm_adjustment"), int(score_record.get("auto_rejected", False)),
                    score_record.get("reject_reason"), score_record.get("pros"),
                    score_record.get("cons"), score_record.get("greenfield"),
                    score_record.get("greenfield_rationale"), score_record.get("pricing_model"),
                    score_record.get("sector"), score_record.get("recommended_angle"),
                    json.dumps(tech), json.dumps(score_record.get("flags", [])),
                    score_record.get("salary_range_detected"), score_record.get("has_fde_model"),
                    score_record.get("match_score"), score_record.get("match_summary"),
                    match_evidence, match_mismatches, match_bullets, match_hooks,
                    score_record.get("tailored_summary", "") or "",
                    differentiators, score_record.get("adjustment_weights_score"),
                    score_record.get("posting_age_days"), score_record.get("posting_date_raw"),
                    norm_url, role_archetype,
                    score_record.get("interview_probability"), score_record.get("interview_probability_rationale"),
                    sections_to_drop,
                    job_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO jobs
                   (company, job_title, url, date_found, final_score, deterministic_score,
                    llm_adjustment, auto_rejected, reject_reason, pros, cons, greenfield,
                    greenfield_rationale, pricing_model, sector, recommended_angle,
                    tech_stack_json, flags_json, salary_range_detected, has_fde_model,
                    match_score, match_summary, match_evidence_json, match_mismatches_json,
                    match_bullets_json, match_hooks_json, match_tailored_summary,
                    differentiator_themes_json,
                    adjustment_weights_score, posting_age_days, posting_date_raw, source_url, role_archetype,
                    interview_probability, interview_probability_rationale,
                    match_sections_to_drop_json, pipeline_stage, status, discovery_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    score_record.get("company", "Unknown"), score_record.get("job_title", ""),
                    url, today, score_record.get("final_score"),
                    score_record.get("deterministic_score"), score_record.get("llm_adjustment"),
                    int(score_record.get("auto_rejected", False)), score_record.get("reject_reason"),
                    score_record.get("pros"), score_record.get("cons"), score_record.get("greenfield"),
                    score_record.get("greenfield_rationale"), score_record.get("pricing_model"),
                    score_record.get("sector"), score_record.get("recommended_angle"),
                    json.dumps(tech), json.dumps(score_record.get("flags", [])),
                    score_record.get("salary_range_detected"), score_record.get("has_fde_model"),
                    score_record.get("match_score"), score_record.get("match_summary"),
                    match_evidence, match_mismatches, match_bullets, match_hooks,
                    score_record.get("tailored_summary", "") or "",
                    differentiators, score_record.get("adjustment_weights_score"),
                    score_record.get("posting_age_days"), score_record.get("posting_date_raw"),
                    norm_url, role_archetype,
                    score_record.get("interview_probability"), score_record.get("interview_probability_rationale"),
                    sections_to_drop,
                    "discovered", "discovered", "manual",
                ),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            """INSERT INTO score_history
               (job_id, final_score, deterministic_score, llm_adjustment,
                match_score, adjustment_weights_score)
               VALUES (?,?,?,?,?,?)""",
            (job_id, score_record.get("final_score"), score_record.get("deterministic_score"),
             score_record.get("llm_adjustment"), score_record.get("match_score"),
             score_record.get("adjustment_weights_score")),
        )
    return job_id


def _url_returns_404(url: str) -> bool:
    """Return True if the URL is definitively closed (HTTP 404 or 410)."""
    try:
        validate_url(url)
        with requests.head(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=5,
            allow_redirects=True,
        ) as resp:
            # Re-validate redirect history
            for r in resp.history:
                validate_url(r.url)
            return resp.status_code in (404, 410)
    except Exception:
        return False


def is_linkedin_job_url(url: str) -> bool:
    """Return True if this is a LinkedIn job URL that scrapers cannot fetch."""
    return "linkedin.com/jobs/" in url.lower() or "linkedin.com/job/" in url.lower()


def extract_provisional_company(url: str) -> str:
    """Best-effort company name from ATS URL slug or LinkedIn URL path."""
    from app.discovery.ats_clients import detect_ats
    ats_type, ats_handle = detect_ats(url)
    if ats_type != "generic" and ats_handle:
        return ats_handle.replace("-", " ").title()
    # LinkedIn company pages: /company/{slug}/jobs/...
    import re
    m = re.search(r"linkedin\.com/company/([a-z0-9_-]+)", url.lower())
    if m:
        return m.group(1).replace("-", " ").title()
    return ""


def _fetch_jd_text(url: str) -> str | None:
    """Attempt to fetch job description text from URL."""
    from bs4 import BeautifulSoup
    from app.discovery.ats_clients import detect_ats

    try:
        validate_url(url)
    except ValueError as e:
        print(f"  URL validation failed for {url}: {e}")
        return None

    # LinkedIn blocks all scrapers — skip the fetch chain entirely
    if is_linkedin_job_url(url):
        print(f"  LinkedIn URL detected — skipping automated fetch: {url}")
        return None

    # Try ATS-specific fetching first
    ats_type, ats_handle = detect_ats(url)
    if ats_type != 'generic':
        try:
            from app.discovery.ats_clients import fetch_greenhouse_jobs, fetch_lever_jobs, fetch_ashby_jobs
            jobs = []
            if ats_type == 'greenhouse':
                jobs = fetch_greenhouse_jobs(ats_handle)
            elif ats_type == 'lever':
                jobs = fetch_lever_jobs(ats_handle)
            elif ats_type == 'ashby':
                jobs = fetch_ashby_jobs(ats_handle)
            
            from urllib.parse import urlparse
            job_id = urlparse(url).path.rstrip('/').split('/')[-1]
            for j in jobs:
                job_url = j.get('url', '')
                if job_url == url or (job_id and job_id in job_url):
                    desc = j.get('description') or ''
                    if desc:
                        return desc
        except Exception as e:
            print(f"  ATS-specific fetch failed for {url}: {e}")

    # Try Jina Reader
    from app.config import JINA_API_KEY
    if JINA_API_KEY:
        import time
        for attempt in range(3):
            try:
                jina_url = f"https://r.jina.ai/{url}"
                validate_url(jina_url)
                jina_headers = {
                    "Authorization": f"Bearer {JINA_API_KEY}",
                    "X-Return-Format": "markdown",
                }
                with requests.get(jina_url, headers=jina_headers, timeout=20, stream=True) as resp:
                    resp.raise_for_status()
                    content = b""
                    for chunk in resp.iter_content(chunk_size=8192):
                        content += chunk
                        if len(content) > MAX_JD_CONTENT_LENGTH:
                            logging.warning(f"Jina content exceeded {MAX_JD_CONTENT_LENGTH} bytes for {url}")
                            break
                    text = content.decode("utf-8", errors="replace").strip()
                    if len(text) > 300:
                        return text[:30000]
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else 0
                if status_code in (401, 402, 403):
                    print(f"  Jina quota/auth error ({status_code}), skipping to next fetcher")
                    break
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  Jina fetch failed for {url}: {e}")
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  Jina fetch failed for {url}: {e}")

    # Firecrawl — handles JS-rendered pages (Workday, Rippling, etc.)
    from app.config import FIRECRAWL_API_KEY
    if FIRECRAWL_API_KEY:
        try:
            from firecrawl import FirecrawlApp
            fc = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
            result = fc.scrape_url(url, formats=["markdown"])
            text = (result.markdown or "").strip()
            if len(text) > 300:
                return text[:30000]
        except Exception as e:
            print(f"  Firecrawl fetch failed for {url}: {e}")

    # Last resort: BeautifulSoup
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        with requests.get(url, headers=headers, timeout=15, stream=True, allow_redirects=True, max_redirects=5) as resp:
            # Re-validate redirect history
            for r in resp.history:
                validate_url(r.url)
            
            resp.raise_for_status()
            content = b""
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > MAX_JD_CONTENT_LENGTH:
                    logging.warning(f"Content exceeded {MAX_JD_CONTENT_LENGTH} bytes for {url}")
                    break
            
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)[:10000]
    except Exception as e:
        print(f"  Generic fetch failed for {url}: {e}")
        return None


def _mark_row_status(creds, row_index: int, status: str) -> None:
    _sheets_put(creds, f"'{TO_EVALUATE_TAB}'!D{row_index}:D{row_index}", [[status]])


def process_new_urls() -> list[dict]:
    """
    Watchdog function. Reads Sheets, finds unscored rows, scores them,
    writes results back. Returns list of processed score records.
    """
    from app.config import is_google_configured
    if not is_google_configured():
        logging.warning("Google Sheets not configured — skipping sheet sync")
        return []
    creds = _get_creds()
    rows = read_to_evaluate()
    processed = []

    for row in rows:
        try:
            url = row.get("Job URL") or row.get("url") or ""
            linkedin_url = row.get("LinkedIn URL") or row.get("linkedin_url") or ""
            score_col = row.get("Final Score") or row.get("final_score") or ""
            existing_notes = row.get("Notes") or row.get("notes") or ""

            if not url:
                continue
            if score_col:
                # Rescue: if the Sheet has a score but no DB record (e.g. save_job_to_db
                # crashed after write_score_to_row succeeded), clear the Sheet score so
                # normal processing re-runs this row from scratch.
                from app.models import normalize_url
                norm = normalize_url(url)
                with get_db() as _conn:
                    _exists = _conn.execute(
                        "SELECT id FROM jobs WHERE source_url = ? OR (source_url IS NULL AND url = ?)",
                        (norm, url),
                    ).fetchone()
                if _exists:
                    continue  # Already in DB — normal skip
                print(f"  Rescue: row {row['row_index']} has Sheet score but no DB record — clearing for reprocess: {url}")
                _sheets_put(creds, f"'{TO_EVALUATE_TAB}'!D{row['row_index']}:E{row['row_index']}", [["", ""]])
                # Fall through to normal processing below

            print(f"Processing: {url}")

            # LinkedIn job URLs cannot be scraped — save stub immediately, skip fetch chain
            if is_linkedin_job_url(url):
                sheet_company_li = row.get("Company") or row.get("company") or ""
                print(f"  LinkedIn URL — saving stub (manual paste required): {url}")
                _mark_row_status(creds, row["row_index"], "LinkedIn — Paste JD to Score")
                save_stub_job_to_db(url, sheet_company_li)
                continue

            if _url_returns_404(url):
                print(f"  URL returned 404/410 — listing likely closed: {url}")
                _mark_row_status(creds, row["row_index"], "Listing closed — 404")
                continue

            sheet_company = row.get("Company") or row.get("company") or ""
            used_fallback = False
            jd_text = _fetch_jd_text(url)
            if not jd_text and linkedin_url:
                print(f"  Primary fetch failed, trying LinkedIn URL: {linkedin_url}")
                jd_text = _fetch_jd_text(linkedin_url)
                if jd_text:
                    used_fallback = True
            if not jd_text:
                print(f"  Fetch failed (both URLs): {url}")
                _mark_row_status(creds, row["row_index"], "Fetch Failed — Stub Saved")
                save_stub_job_to_db(url, sheet_company)
                continue

            score_record = score_job(jd_text)

            # JD was fetched but didn't contain a real job posting — try LinkedIn fallback
            if score_record.get("jd_insufficient"):
                if not used_fallback and linkedin_url:
                    print(f"  JD insufficient on primary URL, trying LinkedIn URL: {linkedin_url}")
                    linkedin_jd_text = _fetch_jd_text(linkedin_url)
                    if linkedin_jd_text:
                        time.sleep(LLM_PACING_SECONDS)
                        score_record = score_job(linkedin_jd_text)
                        if not score_record.get("jd_insufficient"):
                            used_fallback = True
                if score_record.get("jd_insufficient"):
                    print(f"  Unable to score — could not access job listing content: {url}")
                    _mark_row_status(creds, row["row_index"], "Unable to Score — Stub Saved")
                    save_stub_job_to_db(url, sheet_company)
                    continue

            if score_record.get("company", "Unknown") == "Unknown" and score_record.get("job_title", "Unknown") == "Unknown":
                if not used_fallback and linkedin_url:
                    print(f"  Parse error on primary URL, trying LinkedIn URL: {linkedin_url}")
                    linkedin_jd_text = _fetch_jd_text(linkedin_url)
                    if linkedin_jd_text:
                        time.sleep(LLM_PACING_SECONDS)
                        score_record = score_job(linkedin_jd_text)
                        if score_record.get("company", "Unknown") != "Unknown" or score_record.get("job_title", "Unknown") != "Unknown":
                            used_fallback = True
                if score_record.get("company", "Unknown") == "Unknown" and score_record.get("job_title", "Unknown") == "Unknown":
                    print(f"  Parse error (both URLs failed): {url}")
                    _mark_row_status(creds, row["row_index"], "Parse Error — Stub Saved")
                    save_stub_job_to_db(url, sheet_company)
                    continue

            # Run research if score passes threshold (second sequential LLM call — pace it)
            if not score_record.get("auto_rejected") and score_record.get("final_score", 0) >= 6.0:
                company_name = score_record.get("company", "")
                if company_name:
                    time.sleep(LLM_PACING_SECONDS)
                    research = research_company(company_name)
                    score_record["_research"] = research

            write_score_to_row(row["row_index"], score_record)
            if used_fallback:
                _prepend_note(creds, row["row_index"], f"Scored using LinkedIn URL ({linkedin_url}) — primary URL was unreachable.", existing_notes)
            job_id = save_job_to_db(url, score_record)

            # Slack alert for high scores
            if score_record.get("final_score", 0) >= 8.0:
                from app.notifications.slack import send_high_score_alert
                send_high_score_alert(job_id, score_record)

            processed.append(score_record)

            # AI_RULES.md §1: pace between rows to stay under quota
            time.sleep(LLM_PACING_SECONDS)
        except RateLimitedError as e:
            print(f"  Rate limited after retries on row {row.get('row_index', 'unknown')}: {e}")
            try:
                _mark_row_status(creds, row["row_index"], "Rate Limited — Re-queue")
            except Exception:
                pass
            break  # quota exhausted after full backoff — abort remaining rows; cron retries at next window
        except Exception as e:
            print(f"Error processing row {row.get('row_index', 'unknown')}: {e}")
            try:
                _mark_row_status(creds, row["row_index"], f"Error: {str(e)[:60]} — Score Manually")
            except Exception:
                pass
            time.sleep(LLM_PACING_SECONDS)
            continue

    # Reset attempt counter for non-LinkedIn stubs that have exhausted retries.
    # Firecrawl is now in the chain — give these URLs another 3 attempts.
    try:
        _reset_exhausted_stubs()
    except Exception as e:
        print(f"[stub reset] Error during attempt reset: {e}")

    # Retry pass: attempt to score stubs that haven't hit the attempt cap.
    # Runs after new-row processing so quota is available.
    # Cap: up to 10 stubs per sync run; max 3 lifetime attempts per stub.
    try:
        _retry_stubs(processed)
    except Exception as e:
        print(f"[stub retry] Error during retry pass: {e}")

    return processed


def _reset_exhausted_stubs() -> None:
    """Reset jd_fetch_attempts for non-LinkedIn stubs at the attempt cap.
    Called once per sync so Firecrawl (newly added) gets a shot at previously exhausted URLs.
    Only resets stubs where attempts >= 3; leaves LinkedIn stubs (attempts=3 by convention) alone.
    """
    with get_db() as conn:
        conn.execute(
            """UPDATE jobs SET jd_fetch_attempts = 0
               WHERE pipeline_stage = 'identified' AND final_score IS NULL
                 AND jd_fetch_attempts >= 3
                 AND url NOT LIKE '%linkedin.com/jobs/%'
                 AND url NOT LIKE '%linkedin.com/job/%'"""
        )
        reset_count = conn.execute("SELECT changes()").fetchone()[0]
    if reset_count:
        print(f"[stub reset] Reset attempt counter for {reset_count} exhausted non-LinkedIn stub(s)")


def _retry_stubs(processed: list) -> None:
    """Fetch and score stub jobs (pipeline_stage=identified, no score, attempts < 3)."""
    with get_db() as conn:
        stubs = conn.execute(
            """SELECT id, url, company FROM jobs
               WHERE pipeline_stage = 'identified' AND final_score IS NULL
                 AND COALESCE(jd_fetch_attempts, 0) < 3
               ORDER BY date_found DESC LIMIT 10"""
        ).fetchall()
    stubs = [dict(s) for s in stubs]

    if not stubs:
        return

    print(f"[stub retry] Retrying {len(stubs)} stub job(s)...")
    from app.scoring.research import score_job, research_company

    for stub in stubs:
        url = stub.get("url") or ""
        job_id = stub["id"]
        if not url:
            continue
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE jobs SET jd_fetch_attempts = COALESCE(jd_fetch_attempts, 0) + 1 WHERE id = ?",
                    (job_id,),
                )
            jd_text = _fetch_jd_text(url)
            if not jd_text:
                print(f"  [stub retry] Fetch failed for job {job_id}: {url}")
                continue  # no LLM call was made — no pacing needed

            time.sleep(LLM_PACING_SECONDS)
            score_record = score_job(jd_text)

            if not score_record.get("jd_insufficient") and (
                score_record.get("company", "Unknown") != "Unknown"
                or score_record.get("job_title", "Unknown") != "Unknown"
            ):
                if not score_record.get("auto_rejected") and score_record.get("final_score", 0) >= 6.0:
                    company_name = score_record.get("company", "")
                    if company_name:
                        time.sleep(LLM_PACING_SECONDS)
                        research = research_company(company_name)
                        score_record["_research"] = research

                save_job_to_db(url, score_record)
                with get_db() as conn:
                    conn.execute("UPDATE jobs SET jd_fetch_attempts = 0 WHERE id = ?", (job_id,))
                processed.append(score_record)
                print(f"  [stub retry] Scored job {job_id}: {score_record.get('job_title', '?')} @ {score_record.get('company', '?')}")
            else:
                print(f"  [stub retry] Insufficient JD for job {job_id}: {url}")

            time.sleep(LLM_PACING_SECONDS)
        except RateLimitedError as e:
            print(f"  [stub retry] Quota exhausted on job {job_id} — aborting stub retry: {e}")
            break  # remaining stubs will be retried at next sync window
        except Exception as e:
            print(f"  [stub retry] Error on job {job_id}: {e}")
            time.sleep(LLM_PACING_SECONDS)
            continue
