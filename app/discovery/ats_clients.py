"""ATS API clients for automated job discovery."""
import re
import time
import logging
import httpx
from typing import Callable
from urllib.parse import quote, unquote
from app.security.url_guard import validate_url

logger = logging.getLogger(__name__)

def html_to_text(markup: str) -> str:
    """Plain text from job-feed HTML. Greenhouse sends entity-escaped HTML
    (&lt;p&gt;), Ashby and Lever send raw HTML; both come out as readable text."""
    if not markup:
        return ""
    import html as _html
    from bs4 import BeautifulSoup
    text = BeautifulSoup(_html.unescape(markup), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def detect_ats(careers_url: str) -> tuple[str, str]:
    """Detect ATS type and handle from careers URL. Returns (ats_type, ats_handle)."""
    url = careers_url.lower()

    # Greenhouse: boards.greenhouse.io/company or greenhouse.io/jobs
    gh_match = re.search(r'greenhouse\.io/(?:boards/)?([a-z0-9_-]+)', url)
    if gh_match:
        return 'greenhouse', gh_match.group(1)

    # Lever: jobs.lever.co/company
    lv_match = re.search(r'lever\.co/([a-z0-9_-]+)', url)
    if lv_match:
        return 'lever', lv_match.group(1)

    # Ashby: jobs.ashbyhq.com/company — the org's "hosted jobs page name" can
    # contain spaces (e.g. "Trunk Tools"), URL-encoded as %20 in the stored
    # careers_url, so capture the whole path segment and decode it rather
    # than stopping at the first non-slug character.
    ash_match = re.search(r'ashbyhq\.com/([^/?#]+)', url)
    if ash_match:
        return 'ashby', unquote(ash_match.group(1))

    # Workday: {tenant}.wd{N}.myworkdayjobs.com/{site}, optionally with a
    # locale segment (e.g. /en-US/{site}). Handle packs tenant, wd number,
    # and site together since fetch_workday_jobs() needs all three to build
    # the API host and none are recoverable from just one.
    wd_match = re.search(
        r'([a-z0-9_-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([^/?#]+)',
        url,
    )
    if wd_match:
        tenant, wd_num, site = wd_match.groups()
        return 'workday', f"{tenant}|{wd_num}|{unquote(site)}"

    # Teamtailor: career sites often sit on the company's own domain (Lindy:
    # careers.lindy.ai), so store the site's /jobs.rss feed URL as careers_url.
    # The handle is the site root, which fetch_teamtailor_jobs reads the feed from.
    tt_match = re.search(r'^(https?://[^/]+)/jobs\.rss', careers_url.strip(), re.I) \
        or re.search(r'^(https?://[a-z0-9-]+\.teamtailor\.com)', careers_url.strip(), re.I)
    if tt_match:
        return 'teamtailor', tt_match.group(1)

    # SmartRecruiters: jobs.smartrecruiters.com/{company} (ServiceNow, 2026-09-25).
    # The public API is case-insensitive on the company identifier.
    sr_match = re.search(r'(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9_-]+)', url)
    if sr_match:
        return 'smartrecruiters', sr_match.group(1)

    return 'generic', ''


def fetch_greenhouse_jobs(handle: str) -> list[dict]:
    """Fetch all jobs from Greenhouse public API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"
    try:
        validate_url(url)
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for j in data.get('jobs', []):
            jobs.append({
                'title': j.get('title', ''),
                'url': j.get('absolute_url', ''),
                'description': j.get('content', ''),
                'posted_at': j.get('updated_at', ''),
                'location': j.get('location', {}).get('name', '') if isinstance(j.get('location'), dict) else '',
            })
        return jobs
    except Exception as e:
        logger.error(f"Greenhouse fetch failed for {handle}: {e}")
        return []


def fetch_lever_jobs(handle: str) -> list[dict]:
    """Fetch all jobs from Lever public API."""
    url = f"https://api.lever.co/v0/postings/{handle}?mode=json"
    try:
        validate_url(url)
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.warning(f"Lever returned no job list for {handle}: {str(data)[:120]}")
            return []
        jobs = []
        for j in data:
            # Use Lever's plain-text fields. descriptionBody is a nested object on
            # some boards and a plain string on others (Tinybird, 2026-09-24), and
            # the nested read crashed the whole board.
            desc_parts = [j.get('descriptionPlain') or '']
            for lst in j.get('lists') or []:
                if isinstance(lst, dict):
                    desc_parts.append(f"{lst.get('text', '')}: {html_to_text(lst.get('content', ''))}")
            desc_parts.append(j.get('additionalPlain') or '')
            jobs.append({
                'title': j.get('text', ''),
                'url': j.get('hostedUrl', ''),
                'description': '\n'.join(p for p in desc_parts if p.strip()),
                'posted_at': '',
                'location': j.get('categories', {}).get('location', ''),
            })
        return jobs
    except Exception as e:
        logger.error(f"Lever fetch failed for {handle}: {e}")
        return []


def fetch_ashby_jobs(handle: str) -> list[dict]:
    """Fetch jobs from Ashby GraphQL API.

    As of 8/2026, jobBoardWithTeams.jobPostings only returns lightweight
    briefs (id/title/employmentType/locationName) — Ashby dropped isListed
    and jobRequisition.description from that type, which silently zeroed
    out every Ashby company's discovery yield (query errored, caught, and
    swallowed by the except below). Full JD text now requires a second
    per-posting `jobPosting` query.
    """
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    list_query = """
    query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
      jobBoard: jobBoardWithTeams(
        organizationHostedJobsPageName: $organizationHostedJobsPageName
      ) {
        jobPostings {
          id title employmentType
          locationName
        }
      }
    }
    """
    detail_query = """
    query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
      jobPosting(
        organizationHostedJobsPageName: $organizationHostedJobsPageName
        jobPostingId: $jobPostingId
      ) {
        descriptionHtml
      }
    }
    """
    try:
        validate_url(url)
        resp = httpx.post(url, json={
            'operationName': 'ApiJobBoardWithTeams',
            'query': list_query,
            'variables': {'organizationHostedJobsPageName': handle}
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        board = (data.get('data') or {}).get('jobBoard')
        if board is None:
            # Ashby answers 200 with jobBoard: null for an unknown board name, and
            # the old chained .get() crashed on it (Lindy, 2026-09-24).
            logger.warning(f"No Ashby job board named {handle!r}; check the company's careers_url")
            return []
        postings = board.get('jobPostings') or []
        jobs = []
        for j in postings:
            posting_id = j.get('id', '')
            desc = ''
            try:
                # Ashby's public endpoint rate-limits (429) under rapid
                # sequential N+1 detail requests — boards with 100+ postings
                # (e.g. EliseAI) hit it within a couple dozen calls otherwise.
                time.sleep(0.3)
                d_resp = httpx.post(url, json={
                    'operationName': 'ApiJobPosting',
                    'query': detail_query,
                    'variables': {
                        'organizationHostedJobsPageName': handle,
                        'jobPostingId': posting_id,
                    }
                }, timeout=15)
                d_resp.raise_for_status()
                d_data = d_resp.json()
                posting = d_data.get('data', {}).get('jobPosting') or {}
                desc = posting.get('descriptionHtml', '') or ''
            except Exception as e:
                logger.warning(f"Ashby description fetch failed for {handle}/{posting_id}: {e}")
            jobs.append({
                'title': j.get('title', ''),
                'url': f"https://jobs.ashbyhq.com/{quote(handle)}/{posting_id}",
                'description': desc,
                'posted_at': '',
                'location': j.get('locationName', ''),
            })
        return jobs
    except Exception as e:
        logger.error(f"Ashby fetch failed for {handle}: {e}")
        return []


def fetch_ashby_description(handle: str, posting_id: str) -> str:
    """One Ashby posting's description HTML, without reading the whole board."""
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    query = """
    query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
      jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,
                 jobPostingId: $jobPostingId) { descriptionHtml }
    }
    """
    try:
        validate_url(url)
        resp = httpx.post(url, json={
            'operationName': 'ApiJobPosting', 'query': query,
            'variables': {'organizationHostedJobsPageName': handle, 'jobPostingId': posting_id},
        }, timeout=15)
        resp.raise_for_status()
        posting = (resp.json().get('data') or {}).get('jobPosting') or {}
        return posting.get('descriptionHtml') or ''
    except Exception as e:
        logger.warning(f"Ashby description fetch failed for {handle}/{posting_id}: {e}")
        return ''


def fetch_smartrecruiters_jobs(handle: str, want: Callable[[str], bool] | None = None,
                               max_postings: int = 1000) -> list[dict]:
    """Fetch jobs from the SmartRecruiters public API.

    The list endpoint pages 100 at a time and carries titles only; each description
    is a separate detail call. Big boards (ServiceNow has ~700 postings) would take
    minutes, so when `want` is given only matching titles are returned, with their
    descriptions. Without `want`, every title comes back with no description.
    """
    base = f"https://api.smartrecruiters.com/v1/companies/{quote(handle)}/postings"
    jobs: list[dict] = []
    try:
        validate_url(base)
        offset = 0
        while offset < max_postings:
            resp = httpx.get(base, params={"limit": 100, "offset": offset}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            page = data.get("content") or []
            for p in page:
                title = p.get("name", "")
                if want is not None and not want(title):
                    continue
                desc = ""
                if want is not None:
                    try:
                        time.sleep(0.3)  # be polite to the detail endpoint
                        d = httpx.get(f"{base}/{p['id']}", timeout=15)
                        d.raise_for_status()
                        sections = ((d.json().get("jobAd") or {}).get("sections") or {})
                        desc = "\n".join(
                            f"{(s or {}).get('title', '')}\n{(s or {}).get('text', '')}"
                            for s in sections.values() if isinstance(s, dict)
                        )
                    except Exception as e:
                        logger.warning(f"SmartRecruiters description fetch failed for {handle}/{p.get('id')}: {e}")
                loc = p.get("location") or {}
                jobs.append({
                    "title": title,
                    "url": f"https://jobs.smartrecruiters.com/{handle}/{p['id']}",
                    "description": desc,
                    "posted_at": p.get("releasedDate", ""),
                    "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
                                + (" (remote)" if loc.get("remote") else ""),
                })
            offset += len(page)
            if not page or offset >= (data.get("totalFound") or 0):
                break
        return jobs
    except Exception as e:
        logger.error(f"SmartRecruiters fetch failed for {handle}: {e}")
        return jobs


def fetch_teamtailor_jobs(site_root: str) -> list[dict]:
    """Fetch jobs from a Teamtailor career site's public RSS feed ({site}/jobs.rss),
    which carries every open role with its full description."""
    import xml.etree.ElementTree as ET
    url = f"{site_root.rstrip('/')}/jobs.rss"
    try:
        validate_url(url)
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        jobs = []
        for item in root.iter("item"):
            jobs.append({
                "title": (item.findtext("title") or "").strip(),
                "url": (item.findtext("link") or "").strip(),
                "description": item.findtext("description") or "",
                "posted_at": item.findtext("pubDate") or "",
                "location": "",
            })
        return jobs
    except Exception as e:
        logger.error(f"Teamtailor fetch failed for {site_root}: {e}")
        return []


def fetch_workday_jobs(handle: str, want: Callable[[str], bool] | None = None) -> list[dict]:
    """Fetch jobs from a Workday public CXS API.

    No API key or JS rendering needed — {tenant}.wd{N}.myworkdayjobs.com
    exposes a plain JSON search endpoint plus a per-posting detail endpoint
    for full JD text, same two-call shape as Ashby. Paginated.

    Workday rejects list pages larger than 20 with a 400 (verified 2026-09-25;
    the old page size of 50 silently zeroed every Workday company). With `want`,
    every title is listed (cheap) and descriptions are fetched only for matches,
    so large tenants (Salesforce, Palo Alto: ~1,500 postings) are covered. Without
    `want`, the first WORKDAY_MAX_JOBS postings come back with descriptions.
    """
    WORKDAY_MAX_JOBS = 200 if want is None else 2000
    PAGE_SIZE = 20
    try:
        tenant, wd_num, site = handle.split('|', 2)
    except ValueError:
        logger.error(f"Workday fetch failed: malformed handle '{handle}'")
        return []

    base = f"https://{tenant}.wd{wd_num}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    public_base = f"https://{tenant}.wd{wd_num}.myworkdayjobs.com/{site}"
    jobs = []
    try:
        offset = 0
        total = None
        while offset < WORKDAY_MAX_JOBS and (total is None or offset < total):
            list_url = f"{base}/jobs"
            validate_url(list_url)
            resp = httpx.post(
                list_url,
                json={"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            total = data.get('total', 0)
            postings = data.get('jobPostings', []) or []
            if not postings:
                break
            for j in postings:
                if want is not None and not want(j.get('title', '')):
                    continue
                external_path = j.get('externalPath', '')
                desc = ''
                try:
                    time.sleep(0.2)
                    detail_url = f"{base}{external_path}"
                    validate_url(detail_url)
                    d_resp = httpx.get(detail_url, timeout=15)
                    d_resp.raise_for_status()
                    posting = d_resp.json().get('jobPostingInfo', {}) or {}
                    desc = posting.get('jobDescription', '') or ''
                except Exception as e:
                    logger.warning(f"Workday description fetch failed for {handle}{external_path}: {e}")
                jobs.append({
                    'title': j.get('title', ''),
                    'url': f"{public_base}{external_path}",
                    'description': desc,
                    'posted_at': j.get('postedOn', ''),
                    'location': j.get('locationsText', ''),
                })
            offset += PAGE_SIZE
        return jobs
    except Exception as e:
        logger.error(f"Workday fetch failed for {handle}: {e}")
        return jobs


def fetch_generic_jobs(careers_url: str, want: Callable[[str], bool] | None = None) -> list[dict]:
    """
    Fallback scraper for companies without a known ATS.
    Looks for links containing 'job', 'position', 'open-role' etc.
    This is a shallow scan — actual Playwright scraping is preferred for production.
    """
    from bs4 import BeautifulSoup
    try:
        validate_url(careers_url)
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        resp = httpx.get(careers_url, headers=headers, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        
        # Check if we were redirected to a known ATS
        ats_type, ats_handle = detect_ats(str(resp.url))
        if ats_type != 'generic':
            return fetch_jobs_for_company(ats_type, ats_handle, str(resp.url), want=want)

        soup = BeautifulSoup(resp.text, "html.parser")
        jobs = []
        
        # Look for links that might be jobs
        job_keywords = ['job', 'position', 'opening', 'career', 'role', 'apply']
        for link in soup.find_all('a', href=True):
            href = link['href']
            text = link.get_text(strip=True)
            
            # Simple heuristic: link text or href contains keywords + some length constraints
            if any(k in href.lower() or k in text.lower() for k in job_keywords):
                # Avoid common false positives
                if len(text) > 5 and len(text) < 100:
                    # Resolve relative URLs
                    full_url = href
                    if not href.startswith(('http://', 'https://')):
                        from urllib.parse import urljoin
                        full_url = urljoin(careers_url, href)
                        
                    # SSRF Protection for resolved URL
                    try:
                        validate_url(full_url)
                    except ValueError:
                        continue
                        
                    jobs.append({
                        'title': text,
                        'url': full_url,
                        'description': '', # Generic scraper doesn't fetch JD text yet
                        'posted_at': '',
                        'location': '',
                    })
        return jobs
    except Exception as e:
        logger.error(f"Generic fetch failed for {careers_url}: {e}")
        return []


def fetch_jobs_for_company(ats_type: str, ats_handle: str, careers_url: str,
                           want: Callable[[str], bool] | None = None) -> list[dict]:
    """Dispatch to correct ATS client. Returns list of job dicts.

    `want(title)` lets a client with per-posting detail calls skip descriptions for
    titles the caller will drop anyway. Only SmartRecruiters uses it today.
    """
    if ats_type == 'smartrecruiters':
        return fetch_smartrecruiters_jobs(ats_handle, want)
    if ats_type == 'teamtailor':
        return fetch_teamtailor_jobs(ats_handle)
    if ats_type == 'greenhouse':
        return fetch_greenhouse_jobs(ats_handle)
    elif ats_type == 'lever':
        return fetch_lever_jobs(ats_handle)
    elif ats_type == 'ashby':
        return fetch_ashby_jobs(ats_handle)
    elif ats_type == 'workday':
        return fetch_workday_jobs(ats_handle, want)
    elif ats_type == 'generic' and careers_url:
        return fetch_generic_jobs(careers_url, want)
    else:
        logger.warning(f"No ATS client for type '{ats_type}' and no URL provided")
        return []
