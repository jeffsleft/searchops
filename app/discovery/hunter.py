"""Job discovery orchestrator — runs daily to find matching roles at target companies."""
import json
import logging
import yaml
import requests as _req
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.models import get_db
from app.discovery.ats_clients import fetch_jobs_for_company
from app.discovery.matcher import passes_title_filter, generate_fit_analysis
from app.providers import get_provider
from app.security.url_guard import validate_url
from app.scoring.schemas import DiscoveryHunterResult

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "hunt_targets.yaml"

# Non-Tier-A companies are only re-scanned if last_scanned is older than this.
# Tier A is exempt (scanned every run). See run_discovery_scan().
NON_TIER_A_RESCAN_DAYS = 3


def _load_yaml_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _seed_title_filters_if_empty(conn) -> None:
    """Populate title_filters from YAML if the table is empty."""
    count = conn.execute("SELECT COUNT(*) FROM title_filters").fetchone()[0]
    if count > 0:
        return
    config = _load_yaml_config()
    tf = config.get("title_filters", {})
    for value in tf.get("positive", []):
        try:
            conn.execute("INSERT OR IGNORE INTO title_filters (filter_type, value) VALUES (?,?)", ("positive", value))
        except Exception:
            pass
    for value in tf.get("negative", []):
        try:
            conn.execute("INSERT OR IGNORE INTO title_filters (filter_type, value) VALUES (?,?)", ("negative", value))
        except Exception:
            pass


def load_hunt_config() -> dict:
    """Load hunt config. title_filters come from DB (seeds from YAML on first run); other sections from YAML."""
    config = _load_yaml_config()
    try:
        with get_db() as conn:
            _seed_title_filters_if_empty(conn)
            rows = conn.execute(
                "SELECT filter_type, value FROM title_filters WHERE enabled=1 ORDER BY filter_type, value"
            ).fetchall()
        if rows:
            pos = [r["value"] for r in rows if r["filter_type"] == "positive"]
            neg = [r["value"] for r in rows if r["filter_type"] == "negative"]
            config["title_filters"] = {"positive": pos, "negative": neg}
    except Exception as e:
        logger.warning("Could not load title_filters from DB, using YAML: %s", e)
    return config


def run_discovery_scan() -> dict:
    """
    Scan all hunt-enabled companies for new job postings.
    Returns summary dict: {scanned: N, new_found: N, errors: N}
    """
    stats = {'scanned': 0, 'new_found': 0, 'errors': 0, 'discovered_via_search': 0}
    new_jobs = []
    config = load_hunt_config()
    
    # -------------------------------------------------------------------------
    # Level 2: Direct ATS Scanning
    # -------------------------------------------------------------------------
    # Cadence: Tier A companies are scanned every run (the scan itself runs
    # daily). The non-Tier-A long tail is only re-scanned if it hasn't been
    # looked at in NON_TIER_A_RESCAN_DAYS — keeps Tier A hot while cutting load
    # and LLM cost on lower-priority targets. last_scanned is an ISO-8601 UTC
    # string, so lexical comparison against an ISO cutoff is chronological.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NON_TIER_A_RESCAN_DAYS)).isoformat()
    with get_db() as db:
        targets = db.execute(
            """SELECT id, name, ats_type, ats_handle, careers_url FROM companies
               WHERE hunt_enabled = 1
                 AND (tier_a = 1 OR last_scanned IS NULL OR last_scanned < ?)
               ORDER BY tier_a DESC, last_scanned ASC NULLS FIRST""",
            (cutoff,),
        ).fetchall()

    for company in targets:
        company_id = company['id']
        company_name = company['name']
        ats_type = company['ats_type'] or 'unknown'
        ats_handle = company['ats_handle'] or ''
        careers_url = company['careers_url'] or ''

        stats['scanned'] += 1

        try:
            if careers_url:
                validate_url(careers_url)
            raw_jobs = fetch_jobs_for_company(ats_type, ats_handle, careers_url)
        except Exception as e:
            logger.error(f"Scan failed for {company_name}: {e}")
            stats['errors'] += 1
            with get_db() as db:
                db.execute(
                    "UPDATE companies SET scan_error=?, last_scanned=? WHERE id=?",
                    (str(e), datetime.now(timezone.utc).isoformat(), company_id)
                )
            continue

        for job in raw_jobs:
            title = job.get('title', '')
            url = job.get('url', '')

            if not title or not url:
                continue

            # SSRF Protection
            try:
                validate_url(url)
            except ValueError:
                continue

            # Title keyword filter
            if not passes_title_filter(title, config.get('title_filters')):
                continue

            # Dedup check
            with get_db() as db:
                existing = db.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
            if existing:
                continue

            # Generate fit analysis
            fit = generate_fit_analysis(title, job.get('description', ''), company_name)

            # Insert as discovered
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as db:
                db.execute("""
                    INSERT INTO jobs (
                        company_id, company, job_title, url, pipeline_stage, discovery_source,
                        fit_bullets, lightweight_score, date_found, date_added
                    ) VALUES (?, ?, ?, ?, 'discovered', 'hunter', ?, ?, ?, ?)
                """, (
                    company_id,
                    company_name,
                    title,
                    url,
                    json.dumps(fit.get('fit_bullets', [])),
                    fit.get('preliminary_score', 5.0),
                    now,
                    now
                ))

            new_jobs.append({'company': company_name, 'title': title,
                             'score': fit.get('preliminary_score', 5.0)})
            stats['new_found'] += 1
            logger.info(f"Discovered: {company_name} — {title}")

        # Update last_scanned
        with get_db() as db:
            db.execute(
                "UPDATE companies SET last_scanned=?, scan_error=NULL WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), company_id)
            )

        # Refresh the company's match summary (count + best score) after the scan.
        try:
            from app.services.discovery_service import recompute_company_match_summary
            recompute_company_match_summary(company_id)
        except Exception as e:
            logger.warning("recompute_company_match_summary failed for %s: %s", company_id, e)

    # -------------------------------------------------------------------------
    # Level 3: Broad Discovery (Search Dorks)
    # -------------------------------------------------------------------------
    search_queries = config.get("search_queries", [])
    if search_queries:
        print(f"[hunter] Running {len(search_queries)} search dorks for broad discovery.")
        llm = get_provider()
        for query_spec in search_queries:
            name = query_spec.get("name")
            query = query_spec.get("query")
            
            prompt = f"""Use Google Search to find active job listings matching this dork: {query}
            
            Return a JSON list of objects, each with:
            - company: the company name
            - title: the job title
            - url: the direct job board URL
            
            Focus on senior GTM Ops, RevOps, and CS Ops roles. Skip listings that are clearly old or expired.
            Return ONLY a JSON array."""
            
            try:
                # Use generate_json if available, or just generate and parse
                if hasattr(llm, 'generate_json'):
                    results_raw = llm.generate_json(prompt, web_search=True)
                else:
                    raw = llm.generate(prompt, web_search=True, json_mode=True)
                    results_raw = json.loads(raw)
                
                try:
                    # hunter expects a list, so we might need to handle the dict wrapper
                    if isinstance(results_raw, dict) and "jobs" in results_raw:
                         results_raw = results_raw["jobs"]
                    
                    if isinstance(results_raw, list):
                        results = [j.dict() for j in DiscoveryHunterResult(jobs=results_raw).jobs]
                    else:
                        results = []
                except Exception as e:
                    logger.error(f"DiscoveryHunterResult validation failed: {e}")
                    results = results_raw if isinstance(results_raw, list) else []

                for job in results:
                    co_name = job.get("company")
                    title = job.get("title")
                    url = job.get("url")
                    
                    if not co_name or not title or not url:
                        continue
                        
                    # SSRF Protection
                    try:
                        validate_url(url)
                    except ValueError:
                        continue

                    if not passes_title_filter(title, config.get('title_filters')):
                        continue
                        
                    with get_db() as db:
                        existing = db.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
                    if existing:
                        continue
                        
                    # Quick liveness check — skip 404/410 URLs before inserting
                    try:
                        _head = _req.head(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4, allow_redirects=True)
                        if _head.status_code in (404, 410):
                            print(f"[hunter] Skipping closed listing ({_head.status_code}): {url}")
                            continue
                    except Exception:
                        pass  # Network error — proceed optimistically

                    print(f"[hunter] Search discovered new role: {co_name} — {title}")

                    # For search results, we don't have JD text immediately, so fit analysis might be shallow
                    fit = generate_fit_analysis(title, "", co_name)
                    
                    now = datetime.now(timezone.utc).isoformat()
                    with get_db() as db:
                        db.execute("""
                            INSERT INTO jobs (
                                company, job_title, url, pipeline_stage, discovery_source,
                                fit_bullets, lightweight_score, date_found, date_added
                            ) VALUES (?, ?, ?, 'discovered', 'search_dork', ?, ?, ?, ?)
                        """, (
                            co_name, title, url, json.dumps(fit.get('fit_bullets', [])),
                            fit.get('preliminary_score', 5.0), now, now
                        ))
                    
                    new_jobs.append({'company': co_name, 'title': title,
                                     'score': fit.get('preliminary_score', 5.0)})
                    stats['discovered_via_search'] += 1
                    
            except Exception as e:
                logger.error(f"Search dork failed for {name}: {e}")

    # Send Slack notification if new jobs found
    if new_jobs:
        try:
            from app.notifications.slack import send_discovery_notification
            send_discovery_notification(new_jobs)
        except Exception as e:
            logger.warning(f"Slack notification failed: {e}")

    logger.info(f"Discovery scan complete: {stats}")
    return stats
