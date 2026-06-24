"""ATS API clients for automated job discovery."""
import re
import logging
import httpx
from app.security.url_guard import validate_url

logger = logging.getLogger(__name__)

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

    # Ashby: jobs.ashbyhq.com/company
    ash_match = re.search(r'ashbyhq\.com/([a-z0-9_-]+)', url)
    if ash_match:
        return 'ashby', ash_match.group(1)

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
        jobs = []
        for j in data:
            # Lever description is nested
            desc_parts = []
            for section in j.get('descriptionBody', {}).get('body', []):
                if isinstance(section, dict) and section.get('text'):
                    desc_parts.append(section['text'])
            jobs.append({
                'title': j.get('text', ''),
                'url': j.get('hostedUrl', ''),
                'description': ' '.join(desc_parts),
                'posted_at': '',
                'location': j.get('categories', {}).get('location', ''),
            })
        return jobs
    except Exception as e:
        logger.error(f"Lever fetch failed for {handle}: {e}")
        return []


def fetch_ashby_jobs(handle: str) -> list[dict]:
    """Fetch jobs from Ashby GraphQL API."""
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    query = """
    query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
      jobBoard: jobBoardWithTeams(
        organizationHostedJobsPageName: $organizationHostedJobsPageName
      ) {
        jobPostings {
          id title isListed employmentType
          locationName
          jobRequisition { description }
        }
      }
    }
    """
    try:
        validate_url(url)
        resp = httpx.post(url, json={
            'operationName': 'ApiJobBoardWithTeams',
            'query': query,
            'variables': {'organizationHostedJobsPageName': handle}
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        postings = data.get('data', {}).get('jobBoard', {}).get('jobPostings', []) or []
        jobs = []
        for j in postings:
            if not j.get('isListed', True):
                continue
            desc = ''
            req = j.get('jobRequisition')
            if req and isinstance(req, dict):
                desc = req.get('description', '')
            jobs.append({
                'title': j.get('title', ''),
                'url': f"https://jobs.ashbyhq.com/{handle}/{j.get('id', '')}",
                'description': desc,
                'posted_at': '',
                'location': j.get('locationName', ''),
            })
        return jobs
    except Exception as e:
        logger.error(f"Ashby fetch failed for {handle}: {e}")
        return []


def fetch_generic_jobs(careers_url: str) -> list[dict]:
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
            return fetch_jobs_for_company(ats_type, ats_handle, str(resp.url))

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


def fetch_jobs_for_company(ats_type: str, ats_handle: str, careers_url: str) -> list[dict]:
    """Dispatch to correct ATS client. Returns list of job dicts."""
    if ats_type == 'greenhouse':
        return fetch_greenhouse_jobs(ats_handle)
    elif ats_type == 'lever':
        return fetch_lever_jobs(ats_handle)
    elif ats_type == 'ashby':
        return fetch_ashby_jobs(ats_handle)
    elif ats_type == 'generic' and careers_url:
        return fetch_generic_jobs(careers_url)
    else:
        logger.warning(f"No ATS client for type '{ats_type}' and no URL provided")
        return []
