"""JD fetching utilities — URL validation, ATS-specific fetching, Jina/Firecrawl/BS4 chain."""
import logging
import requests

from app.security.url_guard import validate_url

LLM_PACING_SECONDS = 5
MAX_JD_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB


def is_linkedin_job_url(url: str) -> bool:
    """Return True if this is a LinkedIn job URL that scrapers cannot fetch."""
    return "linkedin.com/jobs/" in url.lower() or "linkedin.com/job/" in url.lower()


def extract_provisional_company(url: str) -> str:
    """Best-effort company name from ATS URL slug or LinkedIn URL path."""
    from app.discovery.ats_clients import detect_ats
    ats_type, ats_handle = detect_ats(url)
    if ats_type != "generic" and ats_handle:
        return ats_handle.replace("-", " ").title()
    import re
    m = re.search(r"linkedin\.com/company/([a-z0-9_-]+)", url.lower())
    if m:
        return m.group(1).replace("-", " ").title()
    return ""


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
            for r in resp.history:
                validate_url(r.url)
            return resp.status_code in (404, 410)
    except Exception:
        return False


def _fetch_jd_text(url: str) -> str | None:
    """Attempt to fetch job description text from URL."""
    from bs4 import BeautifulSoup
    from app.discovery.ats_clients import detect_ats

    try:
        validate_url(url)
    except ValueError as e:
        print(f"  URL validation failed for {url}: {e}")
        return None

    if is_linkedin_job_url(url):
        print(f"  LinkedIn URL detected — skipping automated fetch: {url}")
        return None

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

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        with requests.get(url, headers=headers, timeout=15, stream=True, allow_redirects=True, max_redirects=5) as resp:
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


def _reset_exhausted_stubs() -> None:
    """Reset jd_fetch_attempts for non-LinkedIn stubs at the attempt cap."""
    from app.models import get_db
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
    import time
    from app.models import get_db
    from app.scoring.research import score_job, research_company
    from app.providers.gemini import RateLimitedError
    from app.jobs.persist import save_job_to_db

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
                continue

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

                save_job_to_db(url, score_record, jd_text=jd_text)
                with get_db() as conn:
                    conn.execute("UPDATE jobs SET jd_fetch_attempts = 0 WHERE id = ?", (job_id,))
                processed.append(score_record)
                print(f"  [stub retry] Scored job {job_id}: {score_record.get('job_title', '?')} @ {score_record.get('company', '?')}")
            else:
                print(f"  [stub retry] Insufficient JD for job {job_id}: {url}")

            time.sleep(LLM_PACING_SECONDS)
        except RateLimitedError as e:
            print(f"  [stub retry] Quota exhausted on job {job_id} — aborting stub retry: {e}")
            break
        except Exception as e:
            print(f"  [stub retry] Error on job {job_id}: {e}")
            time.sleep(LLM_PACING_SECONDS)
            continue
