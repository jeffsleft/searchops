import html as _html
import json
import logging
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path

from app.auth import AuthMiddleware, SESSION_COOKIE, create_session_token, login_page, verify_session_token
from app.config import load_profile, HIGH_SCORE_THRESHOLD, APP_PASSWORD, USAGE_TRACKING_ENABLED
from app.models import get_db, log_usage_event, log_error_event
from app.observability import get_logger, new_request_id, set_request_id, reset_request_id
from app.scoring.engine import classify_score
from app.scoring.research import research_interviewer, coach_interview
from app.providers import get_provider
from app.interview.drive_sync import append_questions_to_prep_doc
from app.pipeline.tracker import (
    STAGES, advance_stage,
    I_DECLINED_REASONS, THEY_DECLINED_REASONS, JOB_CLOSED_REASONS, DUPLICATE_REASONS,
)
from app.pipeline.followups import get_followups_due
from app.pipeline.calibration import compute_calibration
from app.pipeline.patterns import compute_patterns
from app.services.calibration_service import record_outcome, get_calibration_summary, get_job_outcomes
from app.recruiters.crm import add_recruiter, log_contact, get_all_recruiters, get_stale_recruiters
from app.services.research_service import do_research_company, derive_signals
from app.services.scoring_service import (
    persist_score_record_to_job, score_job_from_text_and_persist,
    score_job_from_url_and_persist, handle_job_add_with_optional_scoring,
)
from app.services.job_actions import (
    score_new_job_from_input, fetch_and_score_stub, update_job_stage,
)
from app.services.company_actions import (
    import_tier_a_companies, research_companies_batch, scan_target_company,
    refresh_company_matches,
)
from app.services.dashboard_service import build_dashboard_data
from app.services.job_service import build_job_detail_data
from app.services.pipeline_service import build_pipeline_view_data
from app.routes_prep import (
    prep_index, prep_for_job, session_create, session_select, session_update, session_delete,
    sessions_reorder, session_set_hook, session_set_schedule, session_set_interviewers,
    session_set_scratchpad, session_set_transcript, question_to_ask_create, question_to_ask_update,
    question_to_ask_delete, question_they_ask_create, question_they_ask_update, question_they_ask_delete,
    question_they_ask_generate, red_flag_create, red_flag_delete, anchor_pin, anchor_unpin,
    session_draft, transcript_analyze, cheat_sheet_print, prep_palette,
)


VALID_ARCHETYPES = {"GTM Ops", "CS Ops", "RevOps", "Finance Ops", "Strategy", "IC-Heavy", "Other"}

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response


class CSRFValidationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        if request.url.path in ("/login",):
            return await call_next(request)
        csrf_header = request.headers.get("X-Requested-With", "").lower()
        if csrf_header != "xmlhttprequest":
            return JSONResponse(
                {"error": "CSRF validation failed"},
                status_code=403
            )
        return await call_next(request)


class VolumeCommitMiddleware(BaseHTTPMiddleware):
    """Innermost middleware: durably commits the Modal Volume after any mutating
    request that completed without error.

    Root cause fixed here (Session 52/53): every table (jobs, pipeline_history,
    application_outcomes, ...) lives in one SQLite file on the Volume, but the
    `web()` ASGI process never called `volume.commit()` anywhere in its request
    path. A write was immediately visible to that same warm container (so the
    live app rendered it correctly) but never flushed to durable storage — only
    visible to other containers, or a fresh `modal volume get`, once Modal
    happened to commit on its own (container scale-down), which could also lose
    the write to a stale commit from another container in the meantime
    (last-writer-wins). This closes that gap for every current and future
    mutating route, not just `/job/{id}/stage`.

    `commit_fn` is injected (mirrors the existing `batch_research_fn` DI
    pattern) so this stays a no-op off Modal, where SQLite already writes
    straight to local disk and is durable without an extra step.
    """

    def __init__(self, app, commit_fn=None):
        super().__init__(app)
        self._commit_fn = commit_fn

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if (
            self._commit_fn is not None
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and response.status_code < 400
        ):
            import asyncio
            try:
                await asyncio.to_thread(self._commit_fn)
            except Exception:
                get_logger("app.observability").error(
                    "volume commit failed after %s %s", request.method, request.url.path
                )
        return response


_obs_log = get_logger("app.observability")

# Single-user today; the value written to the SaaS-shaped actor_id column when a
# request is authenticated. Multi-tenant later swaps this for a real subject.
DEFAULT_ACTOR = "jeff"


def _actor_id(request: Request) -> str | None:
    """Best-effort actor for the usage/error actor_id column (single-user today)."""
    try:
        tok = request.cookies.get(SESSION_COOKIE)
        if tok and verify_session_token(tok):
            return DEFAULT_ACTOR
    except Exception:
        pass
    return None


def _route_template(request: Request) -> str:
    """Matched route template (e.g. /job/{job_id}/rescore) so ids aggregate.

    starlette 1.3 sets scope['endpoint'] (the handler) but NOT scope['route'],
    so we resolve the template from a cached endpoint→path_format map built off
    the live app.routes. Unmatched (404) requests have no endpoint → '<unmatched>'.
    """
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return "<unmatched>"
    app = request.scope.get("app")
    cache = getattr(app.state, "usage_route_map", None) if app is not None else None
    if cache is None and app is not None:
        cache = {
            r.endpoint: r.path_format
            for r in app.routes
            if hasattr(r, "endpoint") and hasattr(r, "path_format")
        }
        app.state.usage_route_map = cache
    if cache and endpoint in cache:
        return cache[endpoint]
    return getattr(endpoint, "__name__", "<unknown>")


class UsageTrackingMiddleware(BaseHTTPMiddleware):
    """Outermost middleware: assigns a correlation id and records one usage_events
    row per request. Timing wraps the full stack (auth/CSRF included). Never breaks
    a request — capture failures are swallowed."""

    async def dispatch(self, request: Request, call_next):
        rid = new_request_id()
        token = set_request_id(rid)
        request.state.request_id = rid
        start = time.monotonic()
        status = 500  # if call_next raises, the request 500s — record that
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            try:
                path = request.url.path
                if USAGE_TRACKING_ENABLED and not path.startswith("/static") and path != "/favicon.ico":
                    log_usage_event(
                        method=request.method,
                        route_template=_route_template(request),
                        status=status,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        is_hx=request.headers.get("HX-Request", "").lower() == "true",
                        actor_id=_actor_id(request),
                    )
            except Exception:
                pass
            reset_request_id(token)


async def unhandled_exception_handler(request: Request, exc: Exception) -> HTMLResponse:
    """App-level 500 handler: persist one error_events row + one structured log line,
    then return the graceful error page. Runs for genuinely unhandled exceptions only
    (route handlers that catch their own errors never reach here)."""
    rid = getattr(request.state, "request_id", None)
    if rid:
        set_request_id(rid)  # so the ERROR log line below correlates to the error_events row
    try:
        log_error_event(
            request_id=rid,
            method=request.method,
            route_template=_route_template(request),
            exc_type=type(exc).__name__,
            message=str(exc),
            actor_id=_actor_id(request),
        )
    except Exception:
        pass
    _obs_log.error("unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return HTMLResponse(
        '<div style="padding:24px;font-family:system-ui;">Something went wrong. '
        'The error has been logged.</div>',
        status_code=500,
    )


TEMPLATES_DIR = Path(__file__).parent / "templates"
jinja = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render(template: str, **ctx) -> HTMLResponse:
    tmpl = jinja.get_template(template)
    return HTMLResponse(tmpl.render(**ctx))


def _enrich_job(row: dict) -> dict:
    job = dict(row)
    score = job.get("final_score") or 0
    tier = classify_score(score)
    job["tier"] = tier["tier"]
    job["color"] = tier["color"]
    try:
        job["flags_list"] = json.loads(job.get("flags_json") or "[]")
    except Exception:
        job["flags_list"] = []
    try:
        job["tech_stack"] = json.loads(job.get("tech_stack_json") or "{}")
    except Exception:
        job["tech_stack"] = {}
    # Layer 2 match payload
    for col, default in (
        ("match_evidence_json", []),
        ("match_mismatches_json", []),
        ("match_bullets_json", []),
        ("match_hooks_json", []),
        ("differentiator_themes_json", []),
    ):
        try:
            job[col.replace("_json", "")] = json.loads(job.get(col) or "[]")
        except Exception:
            job[col.replace("_json", "")] = default
    # WP-E: full cover letter (object, not list)
    try:
        job["match_cover_letter"] = json.loads(job.get("match_cover_letter_json") or "null")
    except Exception:
        job["match_cover_letter"] = None
    return job


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

# --- Login brute-force throttle (audit 2026-06-11; Volume-backed 2026-06-12) ---
# Per-IP lockout: after _LOGIN_MAX_FAILS failures inside a rolling window,
# further attempts are refused until the failures age out. Backed by the
# login_attempts table on the SQLite Volume so the count is GLOBAL across Modal
# containers (the old in-memory dict was per-container). attempted_at is
# wall-clock epoch seconds (time.time()), required for cross-container/restart
# comparability — do not use time.monotonic() here.
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_S = 900  # 15-minute rolling window (also the effective lockout)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "unknown")


def _login_locked(ip: str) -> bool:
    """True if `ip` has >= _LOGIN_MAX_FAILS failures inside the rolling window.
    Prunes aged-out rows as a side effect to keep the table small."""
    cutoff = time.time() - _LOGIN_WINDOW_S
    with get_db() as conn:
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        n = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND attempted_at >= ?",
            (ip, cutoff),
        ).fetchone()[0]
    return n >= _LOGIN_MAX_FAILS


def _record_login_failure(ip: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO login_attempts (ip, attempted_at) VALUES (?, ?)",
            (ip, time.time()),
        )


def _clear_login_failures(ip: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))


async def login_get(request: Request):
    return login_page()


async def login_post(request: Request):
    ip = _client_ip(request)
    if _login_locked(ip):
        logging.warning(f"[login] lockout active for {ip}")
        return login_page(error="Too many attempts. Wait a few minutes and try again.")
    form = await request.form()
    if secrets.compare_digest(form.get("password", ""), APP_PASSWORD):
        _clear_login_failures(ip)
        token = create_session_token()
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(SESSION_COOKIE, token, max_age=60 * 60 * 24 * 30,
                            httponly=True, samesite="strict", secure=True)
        return response
    _record_login_failure(ip)
    logging.warning(f"[login] failed attempt from {ip}")
    return login_page(error="Incorrect password")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

async def dashboard(request: Request):
    selected_archetype = request.query_params.get("archetype", "")
    if selected_archetype and selected_archetype not in VALID_ARCHETYPES:
        logging.warning(f"Invalid archetype ignored: {selected_archetype}")
        selected_archetype = ""

    data = build_dashboard_data(selected_archetype, _enrich_job)
    return render(
        "dashboard.html",
        request=request,
        live=data['live'],
        high=data['high'],
        recent=data['recent'],
        in_pipeline=data['in_pipeline'],
        interviewing=data['interviewing'],
        avg_score=data['avg_score'],
        outreach_queue=data['outreach_queue'],
        total_scored=data['total_scored'],
        discovered_count=data['discovered_count'],
        selected_archetype=selected_archetype,
        followups_due=data['followups_due'],
        upcoming=data['upcoming'],
        last_scan_time=data['last_scan_time'],
        new_since_last=data['new_since_last'],
        today=str(date.today()),
    )


# ---------------------------------------------------------------------------
# Job scoring
# ---------------------------------------------------------------------------

async def score_job_post(request: Request):
    form = await request.form()
    result = score_new_job_from_input(form.get("url", ""), form.get("jd_text", ""))

    if result["status"] == "missing_input":
        return HTMLResponse('<div class="text-red-400 text-sm">Provide a URL or paste JD text.</div>')
    if result["status"] == "duplicate":
        safe_company = _html.escape(result["company"])
        return HTMLResponse(f'<div class="dim" style="font-size:13px;">Already scored — <a href="/job/{result["job_id"]}">{safe_company}</a></div>')
    if result["status"] == "fetch_failed":
        return HTMLResponse('<div class="text-red-400 text-sm">Could not fetch that URL. Paste the JD text instead.</div>')

    score_record = result["score_record"]
    tier = classify_score(score_record.get("final_score", 0))
    company = _html.escape(str(score_record.get('company', 'Unknown')))
    job_title = _html.escape(str(score_record.get('job_title', '')))
    greenfield = _html.escape(str(score_record.get('greenfield', 'Unknown')))
    angle = _html.escape(str(score_record.get('recommended_angle', '')))
    tier_label = _html.escape(tier['tier'])
    color = _html.escape(tier['color'])
    final_score = float(score_record.get('final_score', 0))
    job_link = f'<a href="/job/{int(score_record["_job_id"])}" class="text-xs text-blue-400 hover:text-blue-300 mt-2 inline-block">Open full detail →</a>' if score_record.get("_job_id") else ''
    html = f"""
    <div class="bg-gray-800 rounded-lg p-4 flex gap-4 items-start mt-2">
      <div class="score-{color} rounded-lg w-14 h-14 flex items-center justify-center font-bold text-xl flex-shrink-0">
        {final_score}
      </div>
      <div>
        <div class="font-semibold text-white">{company} — {job_title}</div>
        <div class="text-xs text-gray-400 mt-1">{tier_label} · Greenfield: {greenfield}</div>
        <div class="text-xs text-gray-400 mt-1">{angle}</div>
        {job_link}
      </div>
    </div>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Job detail
# ---------------------------------------------------------------------------

async def job_detail(request: Request):
    job_id = int(request.path_params["job_id"])
    data = build_job_detail_data(job_id, _enrich_job)
    if not data:
        return HTMLResponse("Job not found", status_code=404)

    return render(
        "job_detail.html",
        request=request,
        job=data['job'],
        contacts=data['contacts'],
        questions=data['questions'],
        questions_answered=data['questions_answered'],
        flags_fired=data['flags_fired'],
        tech_stack=data['tech_stack'],
        all_stages=STAGES,
        i_declined_reasons=I_DECLINED_REASONS,
        they_declined_reasons=THEY_DECLINED_REASONS,
        job_closed_reasons=JOB_CLOSED_REASONS,
        duplicate_reasons=DUPLICATE_REASONS,
        job_outcomes=get_job_outcomes(job_id),
    )


async def job_resume_preview(request: Request):
    """Print-optimized resume view with Layer 2 tailored bullets injected."""
    from app.scoring.resume_parser import load_resume, match_bullet_to_company
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse("Job not found", status_code=404)

    job = dict(row)

    try:
        resume = load_resume()
    except Exception as e:
        logging.warning(f"Resume parse failed: {e}")
        resume = {"header": {"name": "Jeff Beaumont", "tagline": "", "contact": ""}, "sections": []}

    try:
        tailored_bullets = json.loads(job.get("match_bullets_json") or "[]")
        cover_hooks = json.loads(job.get("match_hooks_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        tailored_bullets = []
        cover_hooks = []

    tailored_summary = job.get("match_tailored_summary") or ""

    try:
        sections_to_drop = json.loads(job.get("match_sections_to_drop_json") or "[]")
        if not isinstance(sections_to_drop, list):
            sections_to_drop = []
    except (json.JSONDecodeError, TypeError):
        sections_to_drop = []

    # Map tailored bullets to their matching experience section.
    # New format: [{"company": "GitLab", "bullet": "..."}]
    # Old format (plain string): fall back to keyword matching.
    company_bullets: dict[str, list[str]] = {}
    for item in tailored_bullets:
        if isinstance(item, dict):
            co = item.get("company", "")
            bullet = item.get("bullet", "")
            if co and bullet:
                company_bullets.setdefault(co, []).append(bullet)
        elif isinstance(item, str) and item:
            co = match_bullet_to_company(item) or "GitLab"
            company_bullets.setdefault(co, []).append(item)

    tmpl = jinja.get_template("resume_print.html")
    html = tmpl.render(
        header=resume["header"],
        sections=resume["sections"],
        job=job,
        tailored_bullets=tailored_bullets,
        cover_hooks=cover_hooks,
        company_bullets=company_bullets,
        tailored_summary=tailored_summary,
        sections_to_drop=sections_to_drop,
    )
    return HTMLResponse(html)


async def job_resume_download(request: Request):
    """Download tailored resume as a .docx file."""
    from app.scoring.resume_parser import load_resume, match_bullet_to_company
    from app.resume_docx import build_resume_docx
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse("Job not found", status_code=404)

    job = dict(row)

    try:
        resume = load_resume()
    except Exception as e:
        logging.warning(f"Resume parse failed for docx download: {e}")
        resume = {"header": {"name": "Jeff Beaumont", "tagline": "", "contact": ""}, "sections": []}

    try:
        tailored_bullets = json.loads(job.get("match_bullets_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        tailored_bullets = []

    company_bullets: dict[str, list[str]] = {}
    for item in tailored_bullets:
        if isinstance(item, dict):
            co = item.get("company", "")
            bullet = item.get("bullet", "")
            if co and bullet:
                company_bullets.setdefault(co, []).append(bullet)
        elif isinstance(item, str) and item:
            co = match_bullet_to_company(item) or "GitLab"
            company_bullets.setdefault(co, []).append(item)

    try:
        docx_sections_to_drop = json.loads(job.get("match_sections_to_drop_json") or "[]")
        if not isinstance(docx_sections_to_drop, list):
            docx_sections_to_drop = []
    except (json.JSONDecodeError, TypeError):
        docx_sections_to_drop = []

    docx_bytes = build_resume_docx(
        header=resume["header"],
        sections=resume["sections"],
        company_bullets=company_bullets,
        job_company=job.get("company", ""),
        sections_to_drop=docx_sections_to_drop,
    )

    safe_company = (job.get("company") or "Resume").replace("/", "-").replace("\\", "-")
    filename = f"Jeff Beaumont - {safe_company}.docx"

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _cover_letter_fragment(job_id: int, cl: dict) -> str:
    """HTMX fragment: rendered letter preview + download/print actions."""
    if cl.get("error") and not cl.get("body"):
        return (
            f'<div style="color:var(--tier-pass);font-size:13px;">'
            f'❌ {_html.escape(str(cl["error"])[:160])}</div>'
        )
    v = cl.get("voice") or {}
    voice_removed = max(0, (v.get("before", 0) - v.get("after", 0))) if v.get("enabled") else 0
    return jinja.get_template("components/cover_letter_fragment.html").render(
        job_id=job_id,
        cl=cl,
        voice=v,
        voice_removed=voice_removed,
    )


def _generate_and_store_cover_letter(job_id: int) -> dict:
    """Generate a cover letter for a job and persist it to match_cover_letter_json."""
    from app.scoring.cover_letter import generate_cover_letter
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {"error": "Job not found", "body": []}
    # generate_cover_letter applies the WP-F voice pass internally — don't re-polish here.
    cl = generate_cover_letter(dict(row))
    if cl.get("body"):
        with get_db() as conn:
            conn.execute(
                "UPDATE jobs SET match_cover_letter_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(cl), job_id),
            )
    return cl


async def job_cover_letter_generate(request: Request):
    """Generate (or regenerate) the full cover letter. HTMX-safe — returns a fragment."""
    job_id = int(request.path_params["job_id"])
    try:
        cl = _generate_and_store_cover_letter(job_id)
    except Exception as e:
        logging.error("cover letter generation failed for job %s: %s", job_id, e)
        return HTMLResponse(
            f'<div style="color:var(--tier-pass);font-size:13px;">❌ Error: {_html.escape(str(e)[:120])}</div>'
        )
    return HTMLResponse(_cover_letter_fragment(job_id, cl))


def _load_cover_letter(job: dict) -> dict:
    """Return the stored cover letter, generating + persisting it on first request."""
    try:
        stored = json.loads(job.get("match_cover_letter_json") or "null")
    except (json.JSONDecodeError, TypeError):
        stored = None
    if stored and stored.get("body"):
        return stored
    return _generate_and_store_cover_letter(job["id"])


async def job_cover_letter_preview(request: Request):
    """Print-optimized cover letter view."""
    from app.scoring.resume_parser import load_resume
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse("Job not found", status_code=404)
    job = dict(row)
    cl = _load_cover_letter(job)

    try:
        header = load_resume()["header"]
    except Exception:
        header = {"name": "Jeff Beaumont", "contact": ""}

    tmpl = jinja.get_template("cover_letter_print.html")
    return HTMLResponse(tmpl.render(
        job=job,
        header={"name": header.get("name", ""), "contact": header.get("contact", "")},
        date=datetime.now().strftime("%B %-d, %Y"),
        cl=cl,
    ))


async def job_cover_letter_download(request: Request):
    """Download the tailored cover letter as a .docx file."""
    from app.scoring.resume_parser import load_resume
    from app.resume_docx import build_cover_letter_docx
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse("Job not found", status_code=404)
    job = dict(row)
    cl = _load_cover_letter(job)

    try:
        header = load_resume()["header"]
    except Exception:
        header = {"name": "Jeff Beaumont", "contact": ""}

    docx_bytes = build_cover_letter_docx(
        header={"name": header.get("name", ""), "contact": header.get("contact", "")},
        body=cl.get("body", []),
        date=datetime.now().strftime("%B %-d, %Y"),
        recipient=cl.get("recipient", ""),
        salutation=cl.get("salutation", "Dear Hiring Team,"),
        closing=cl.get("closing", "Sincerely,"),
        signature=header.get("name", ""),
    )
    safe_company = (job.get("company") or "Cover Letter").replace("/", "-").replace("\\", "-")
    filename = f"Jeff Beaumont - {safe_company} - Cover Letter.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def job_kit(request: Request):
    """W2 — Application Kit. Assembly lives in kit_service; this route just renders it."""
    from app.services.kit_service import build_kit_data

    job_id = int(request.path_params["job_id"])
    kit = build_kit_data(job_id, _enrich_job, _load_cover_letter)
    if not kit:
        return HTMLResponse("Job not found", status_code=404)
    return render("kit.html", request=request, **kit)


async def job_kit_download(request: Request):
    """Download the Application Kit brief as a .docx."""
    from app.kit_docx import build_kit_docx
    from app.services.kit_service import build_kit_data

    job_id = int(request.path_params["job_id"])
    kit = build_kit_data(job_id, _enrich_job, _load_cover_letter)
    if not kit:
        return HTMLResponse("Job not found", status_code=404)
    if not kit["gate"]["cleared"]:
        # Don't hand out a brief for a job that hasn't cleared the gate — the gate is the
        # point. Send them back to the view, which names the failing check.
        return RedirectResponse(f"/job/{job_id}/kit", status_code=303)

    docx_bytes = build_kit_docx(kit)
    company = (kit["job"].get("company") or "Kit").replace("/", "-").replace("\\", "-")
    filename = f"Application Kit - {company}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def job_research_panel(request: Request):
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        job = conn.execute("SELECT company FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return HTMLResponse("")
        row = conn.execute("SELECT result_json FROM research_cache WHERE company_name = ?", (job["company"],)).fetchone()
    if not row:
        return HTMLResponse('<div class="text-sm text-gray-500">No research yet. <button hx-post="/job/' + str(job_id) + '/research" hx-swap="outerHTML" hx-target="closest div" class="text-blue-400 hover:text-blue-300">Run research →</button></div>')
    research = json.loads(row["result_json"])
    items = "".join(
        f'<div class="flex justify-between text-sm py-1 border-b border-gray-800 last:border-0"><span class="text-gray-500 capitalize">{_html.escape(str(k).replace("_"," "))}</span><span class="text-gray-300">{_html.escape(str(v))}</span></div>'
        for k, v in research.items() if v and v != "Unknown" and not isinstance(v, bool)
    )
    return HTMLResponse(f'<h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Research</h2>{items}')


def _render_brief_html(content: str) -> str:
    import re as _re
    content = _html.escape(content)
    # Most-specific heading level first so ## doesn't eat the # in ###
    content = _re.sub(r"^### (.+)$", lambda m: f'<h4 style="font-size:10.5px;font-weight:600;color:var(--text-dim);text-transform:uppercase;letter-spacing:.07em;margin:14px 0 4px 0;">{m.group(1)}</h4>', content, flags=_re.MULTILINE)
    content = _re.sub(r"^## (.+)$",  lambda m: f'<h3 style="font-size:13px;font-weight:600;color:var(--text);margin:18px 0 6px 0;">{m.group(1)}</h3>', content, flags=_re.MULTILINE)
    content = _re.sub(r"^# (.+)$",   lambda m: f'<h2 style="font-size:15px;font-weight:700;color:var(--text);margin:0 0 12px 0;">{m.group(1)}</h2>', content, flags=_re.MULTILINE)
    content = _re.sub(r"^- (.+)$",   lambda m: f'<li style="font-size:13px;color:var(--text-muted);margin-left:16px;list-style:disc;">{m.group(1)}</li>', content, flags=_re.MULTILINE)
    content = _re.sub(r'\*\*(.+?)\*\*', lambda m: f'<strong>{m.group(1)}</strong>', content)
    return f'<div style="font-size:13px;line-height:1.7;color:var(--text);">{content}</div>'


async def job_brief_panel(request: Request):
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT content FROM strategy_briefs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse(
            jinja.get_template("components/job_brief_no_brief.html").render(job_id=job_id)
        )
    return HTMLResponse(_render_brief_html(row["content"]))


async def job_generate_brief(request: Request):
    """Generate (or regenerate) the strategy brief for a job. HTMX-safe — returns HTML fragment."""
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse('<div style="color:var(--tier-pass);font-size:13px;">❌ Job not found.</div>')
    try:
        from app.pipeline.strategy_brief import get_or_create_brief
        brief = get_or_create_brief(job_id)
        content = brief.get("content", "") if brief else ""
        if not content:
            return HTMLResponse('<div style="color:var(--tier-pass);font-size:13px;">❌ Brief generation returned empty — check research data.</div>')
        return HTMLResponse(_render_brief_html(content))
    except Exception as e:
        logging.error("generate_brief failed for job %s: %s", job_id, e)
        return HTMLResponse(f'<div style="color:var(--tier-pass);font-size:13px;">❌ Error: {_html.escape(str(e)[:120])}</div>')


async def job_trigger_research(request: Request):
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        job = conn.execute("SELECT company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job:
        from app.scoring.research import research_company, assess_company_fit
        research = research_company(job["company"], force=True)
        assess_company_fit(job["company"], research)
        with get_db() as conn:
            conn.execute(
                """UPDATE jobs SET
                   has_fde_model = ?,
                   timing_signal = ?,
                   timing_signal_rationale = ?
                   WHERE id = ?""",
                (research.get("has_fde_model", "Unknown"),
                 research.get("timing_signal", "Unknown"),
                 research.get("timing_signal_rationale", ""),
                 job_id)
            )
        from app.pipeline.strategy_brief import get_or_create_brief
        get_or_create_brief(job_id)
        from app.questions.bank import seed_questions
        seed_questions(job_id)
    return RedirectResponse(url=f"/job/{job_id}", status_code=302)


async def job_drawer(request: Request):
    """Return a condensed job detail fragment for the right-slide drawer."""
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse('<div class="dim" style="padding:24px;">Job not found.</div>')
    job = _enrich_job(dict(row))

    FLAGS = {
        "windows_penalty":    ("Windows + Teams stack",          -2.0),
        "modern_tech":        ("Mac + Slack stack",              +1.0),
        "experienced_founder":("2nd-time founder",               +1.0),
        "first_time_founder": ("1st-time founder",               -1.0),
        "greenfield":         ("Greenfield / 0→1 opportunity",  +2.0),
        "process_upcycling":  ("Process cleanup only",           -1.0),
        "modern_pricing":     ("Consumption/Outcome pricing",    +1.0),
        "target_sector":      ("Target sector match",            +1.5),
        "moderate_sector":    ("Moderate sector match",          +0.5),
        "low_interest_sector":("Low-interest sector",            -0.5),
        "remote":             ("Fully remote",                   +0.5),
        "churn_burn":         ("CS shrinking, Sales growing",    -1.0),
    }
    flags_fired = [
        {"id": fid, "label": FLAGS[fid][0], "weight": FLAGS[fid][1]}
        for fid in job.get("flags_list", []) if fid in FLAGS
    ]
    return HTMLResponse(jinja.get_template("components/job_drawer.html").render(job=job, flags_fired=flags_fired))


async def job_update_stage(request: Request):
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    new_stage = form.get("to_stage", "")
    result = advance_stage(job_id, new_stage, notes=form.get("notes", ""),
                           decline_reason=form.get("decline_reason", ""))
    if not result["ok"]:
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
    stage_label = STAGES.get(new_stage, {}).get("label", new_stage)
    return JSONResponse({"ok": True, "stage": new_stage, "stage_label": stage_label, "job_id": job_id})


async def job_toggle_ethics(request: Request):
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        current = conn.execute("SELECT ethics_vetted FROM jobs WHERE id = ?", (job_id,)).fetchone()
        new_val = 0 if (current and current["ethics_vetted"]) else 1
        conn.execute("UPDATE jobs SET ethics_vetted = ? WHERE id = ?", (new_val, job_id))

    # Vetting list row — return empty body so HTMX removes the row
    hx_target = request.headers.get("HX-Target", "")
    if hx_target.startswith("job-row-"):
        return HTMLResponse("")

    # JD page — return new button + OOB swap for Key Facts cell
    btn_label = "✓ Ethics vetted" if new_val else "Mark ethics vetted"
    btn_class = "btn btn-sm btn-ghost" if new_val else "btn btn-sm"
    tag = '<span class="tag green">✓ Confirmed</span>' if new_val else '<span class="tag red">Not vetted</span>'
    return HTMLResponse(
        f'<form hx-post="/job/{job_id}/ethics" hx-swap="outerHTML" hx-target="closest form" style="margin:0;">'
        f'<button type="submit" class="{btn_class}">{btn_label}</button>'
        f'</form>'
        f'<dd id="ethics-vetted-cell" hx-swap-oob="true">{tag}</dd>'
    )


async def job_save_notes(request: Request):
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    notes = form.get("notes", "").strip()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))
    return HTMLResponse(
        '<span style="color:var(--accent);font-size:11px;" '
        'hx-swap-oob="true" id="notes-save-indicator">Saved</span>'
    )


async def job_add_contact(request: Request):
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    name = form.get("name","").strip()
    title = form.get("title","").strip()
    linkedin_url = form.get("linkedin_url","").strip()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO contacts (job_id, name, title, linkedin_url) VALUES (?,?,?,?)",
            (job_id, name, title, linkedin_url),
        )
    safe_name = _html.escape(name)
    safe_title = _html.escape(title)
    return HTMLResponse(
        f'<div class="text-xs py-2 border-b border-gray-800"><div class="text-white">{safe_name}</div>'
        f'<div class="text-gray-500">{safe_title}</div></div>'
    )


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

async def companies_list(request: Request):
    tab = request.query_params.get("tab", "Watchlist")
    sort_col = request.query_params.get("sort", "fit_score")
    sort_order = request.query_params.get("order", "desc")

    # Whitelist columns to prevent SQL injection
    allowed_cols = {"fit_score", "need_assessment", "name", "date_added", "funding_stage"}
    if sort_col not in allowed_cols:
        sort_col = "fit_score"
    sort_order = "ASC" if sort_order.lower() == "asc" else "DESC"
    _safe_orders = {
        ("fit_score",      "DESC"): "fit_score DESC NULLS LAST",
        ("fit_score",      "ASC"):  "fit_score ASC NULLS FIRST",
        ("need_assessment","DESC"): "need_assessment DESC NULLS LAST",
        ("need_assessment","ASC"):  "need_assessment ASC NULLS FIRST",
        ("name",           "DESC"): "name DESC NULLS LAST",
        ("name",           "ASC"):  "name ASC NULLS FIRST",
        ("date_added",     "DESC"): "date_added DESC NULLS LAST",
        ("date_added",     "ASC"):  "date_added ASC NULLS FIRST",
        ("funding_stage",  "DESC"): "funding_stage DESC NULLS LAST",
        ("funding_stage",  "ASC"):  "funding_stage ASC NULLS FIRST",
    }
    order_clause = _safe_orders.get((sort_col, sort_order), "fit_score DESC NULLS LAST")

    with get_db() as conn:
        if tab == "All":
            rows = conn.execute(f"SELECT * FROM companies ORDER BY {order_clause}").fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM companies WHERE status = ? ORDER BY {order_clause}", (tab,)).fetchall()
        count_rows = conn.execute("SELECT status, COUNT(*) as n FROM companies GROUP BY status").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

    count_map = {r["status"]: r["n"] for r in count_rows}
    companies = [dict(r) for r in rows]

    return render(
        "companies.html",
        request=request,
        companies=companies,
        tab=tab,
        sort=sort_col,
        order=sort_order.lower(),
        count_map=count_map,
        total=total,
    )


async def companies_add(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return RedirectResponse(url="/companies", status_code=302)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO companies (name, website, sector, funding_stage, why_interesting, date_added)
               VALUES (?,?,?,?,?,?)""",
            (name, form.get("website",""), form.get("sector",""),
             form.get("funding_stage",""), form.get("why_interesting",""), str(date.today())),
        )
        co_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Kick off research in background (fire and forget via redirect)
    return RedirectResponse(url=f"/companies/{co_id}/research-redirect", status_code=302)


async def company_research_redirect(request: Request):
    co_id = int(request.path_params["co_id"])
    with get_db() as conn:
        co = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
    if co:
        try:
            from app.scoring.research import research_company, assess_company_fit
            research = research_company(co["name"])
            fit = assess_company_fit(co["name"], research)

            # Merge fit insights into research for the UI
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
                     research.get("funding_stage", ""), co_id),
                )
        except Exception as e:
            logging.error("Research failed for company %s (%s): %s", co_id, co['name'], e)
    return RedirectResponse(url="/companies", status_code=302)


_do_research_company = do_research_company  # backward-compat alias used by company_trigger_research


async def company_trigger_research(request: Request):
    import asyncio
    co_id = int(request.path_params["co_id"])
    with get_db() as conn:
        co = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
    if not co:
        return HTMLResponse('<div class="dim" style="padding:24px;">Company not found.</div>')
    asyncio.create_task(asyncio.to_thread(_do_research_company, co_id, dict(co)))
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            '<div style="padding:24px;font-size:13px;color:var(--text-muted);">'
            'Research started — takes ~60 seconds. Close and reopen this panel when done.'
            '</div>'
        )
    return RedirectResponse(url=f"/companies/{co_id}/research-redirect", status_code=302)


_derive_signals = derive_signals  # backward-compat alias used by company_detail


async def company_detail(request: Request):
    co_id = int(request.path_params["co_id"])
    with get_db() as conn:
        co = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
    if not co:
        return HTMLResponse('<div class="dim" style="padding:24px;">Company not found.</div>')
    co = dict(co)
    research = {}
    if co.get("research_json"):
        try:
            research = json.loads(co["research_json"])
        except Exception:
            pass
    signals = _derive_signals(research)
    with get_db() as conn:
        match_rows = conn.execute(
            """SELECT id, job_title, date_added, date_found, pipeline_stage,
                      COALESCE(final_score, lightweight_score) AS match_score
                 FROM jobs
                WHERE company_id = ?
                  AND COALESCE(discovery_source,'') LIKE 'hunt%'
                ORDER BY match_score DESC NULLS LAST, COALESCE(date_added, date_found) DESC
                LIMIT 20""",
            (co_id,)
        ).fetchall()
    matches = [dict(r) for r in match_rows]
    return HTMLResponse(jinja.get_template("components/company_detail_panel.html").render(
        co=co, research=research, signals=signals, matches=matches))


async def company_update_status(request: Request):
    co_id = int(request.path_params["co_id"])
    form = await request.form()
    new_status = form.get("status", "Watchlist")
    with get_db() as conn:
        conn.execute("UPDATE companies SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_status, co_id))
    return HTMLResponse("")


async def company_promote(request: Request):
    co_id = int(request.path_params["co_id"])
    form = await request.form()
    job_title = form.get("job_title", "").strip()
    url = form.get("url", "").strip()
    if not job_title:
        return HTMLResponse('<span style="color:var(--tier-skip);font-size:12px;">Job title required.</span>')
    with get_db() as conn:
        co = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
        if not co:
            return HTMLResponse('<span style="color:var(--tier-skip);font-size:12px;">Company not found.</span>')
        conn.execute(
            """INSERT INTO jobs (company, company_id, job_title, url, date_found, pipeline_stage, sector, status)
               VALUES (?, ?, ?, ?, ?, 'identified', ?, 'active')""",
            (co["name"], co_id, job_title, url, str(date.today()), co["sector"] or ""),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return RedirectResponse(url=f"/job/{job_id}", status_code=302)


async def companies_research_batch(request: Request):
    msg = research_companies_batch(request.app.state.batch_research_fn)
    safe_msg = _html.escape(msg)
    return HTMLResponse(f'<span class="dim" style="font-size:12px;">{safe_msg}</span>')


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def pipeline_view(request: Request):
    view = request.query_params.get("view", "kanban")
    selected_archetype = request.query_params.get("archetype", "")
    if selected_archetype and selected_archetype not in VALID_ARCHETYPES:
        logging.warning(f"Invalid archetype ignored: {selected_archetype}")
        selected_archetype = ""

    data = build_pipeline_view_data(selected_archetype, _enrich_job)
    stages = [(code, info["label"]) for code, info in STAGES.items()]
    return render(
        "pipeline.html",
        request=request,
        stages=stages,
        jobs_by_stage=data['jobs_by_stage'],
        stale_items=data['stale_items'],
        total=data['total'],
        active=data['active'],
        selected_archetype=selected_archetype,
        i_declined_reasons=I_DECLINED_REASONS,
        they_declined_reasons=THEY_DECLINED_REASONS,
        job_closed_reasons=JOB_CLOSED_REASONS,
        duplicate_reasons=DUPLICATE_REASONS,
        view=view,
        max_stage_count=data['max_stage_count'],
    )


async def generate_cheatsheet(request: Request):
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    from app.pipeline.strategy_brief import generate_cheatsheet
    result = generate_cheatsheet(job_id, form.get("round",""), form.get("interviewers",""))
    safe_result = _html.escape(str(result))
    return HTMLResponse(f'<div class="whitespace-pre-wrap text-sm text-gray-300">{safe_result}</div>')


async def analyze_transcript_post(request: Request):
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    from app.pipeline.transcript import analyze_transcript
    result = analyze_transcript(
        job_id=job_id,
        raw_transcript=form.get("raw_transcript",""),
        granola_analysis=form.get("granola_analysis") or None,
        contact_name=form.get("contact_name",""),
        contact_title=form.get("contact_title",""),
    )
    analysis = result["gemini_analysis"]
    comparison = result.get("comparison_result")

    parts = ["<div class='space-y-4'>"]
    if analysis.get("key_signals"):
        parts.append("<div><div class='text-xs font-semibold text-gray-400 mb-1'>Key Signals</div>")
        for s in analysis["key_signals"]:
            safe_s = _html.escape(str(s))
            parts.append(f"<div class='text-sm text-gray-300'>· {safe_s}</div>")
        parts.append("</div>")

    if analysis.get("operational_debt_signals"):
        parts.append("<div><div class='text-xs font-semibold text-gray-400 mb-1'>Debt Signals</div>")
        for d in analysis["operational_debt_signals"]:
            color = "red" if d.get("severity") == "High" else "yellow"
            safe_type = _html.escape(str(d.get("type", "")))
            safe_signal = _html.escape(str(d.get("signal", "")))
            parts.append(f"<div class='text-sm text-{color}-300'>· [{safe_type}] {safe_signal}</div>")
        parts.append("</div>")

    if comparison:
        parts.append("<div class='border-t border-gray-700 pt-3'><div class='text-xs font-semibold text-purple-400 mb-1'>Gemini vs Granola</div>")
        if comparison.get("divergences"):
            for div in comparison["divergences"]:
                safe_topic = _html.escape(str(div.get("topic", "")))
                safe_rec = _html.escape(str(div.get("recommendation", "")))
                parts.append(f"<div class='text-xs text-orange-300'>⚠ {safe_topic}: {safe_rec}</div>")
        safe_comp_summary = _html.escape(str(comparison.get('summary','')))
        parts.append(f"<div class='text-xs text-gray-400 mt-1'>{safe_comp_summary}</div></div>")

    q_count = len(analysis.get('unanswered_questions',[]) ) + len(analysis.get('new_questions_to_ask',[]))
    parts.append(f"<div class='text-xs text-gray-500'>Questions added to bank: {q_count}</div>")
    parts.append("</div>")
    return HTMLResponse("".join(parts))


async def research_interviewer_post(request: Request):
    try:
        form = await request.form()
        name = form.get("name", "").strip()
        title = form.get("title", "").strip()
        company = form.get("company", "").strip()
        if not name or not title or not company:
            return HTMLResponse('<div class="text-red-400 text-sm">All fields required (Name, Title, Company).</div>')
        result = research_interviewer(name, title, company)
        parts = ["<div class='space-y-3'>"]
        sections = [
            ("background", "Background", ["summary", "notable_companies", "conversation_approach"]),
            ("leadership_philosophy", "Leadership Philosophy", ["summary", "focus_areas"]),
            ("strategy_perspective", "Strategy Perspective", ["summary"]),
            ("hiring_signals", "Hiring Signals", ["what_they_emphasize", "stated_hiring_views"]),
            ("likely_interview_focus", "Likely Interview Focus", ["probable_topics", "likely_questions", "what_success_looks_like"]),
            ("engagement_strategy", "Engagement Strategy", ["how_to_resonate", "rapport_topics", "questions_to_ask_them"]),
        ]
        for key, label, fields in sections:
            sec = result.get(key)
            if not sec:
                continue
            parts.append('<div class="border border-gray-700 rounded p-3">')
            safe_label = _html.escape(label)
            parts.append(f'<div class="text-xs font-semibold text-gray-400 mb-2">{safe_label}</div>')
            for f in fields:
                val = sec.get(f)
                if not val:
                    continue
                if isinstance(val, list):
                    for item in val[:4]:
                        safe_item = _html.escape(str(item))
                        parts.append(f'<div class="text-xs text-gray-400">• {safe_item}</div>')
                else:
                    safe_val = _html.escape(str(val))
                    parts.append(f'<div class="text-sm text-gray-300">{safe_val}</div>')
            parts.append("</div>")
        parts.append("</div>")
        return HTMLResponse("".join(parts))
    except Exception as e:
        safe_e = _html.escape(str(e))
        return HTMLResponse(f'<div class="text-red-400 text-sm">Error: {safe_e}</div>')


async def coach_interview_post(request: Request):
    try:
        form = await request.form()
        transcript = form.get("transcript", "").strip()
        company = form.get("company", "").strip()
        job_title = form.get("job_title", "").strip()
        interviewer = form.get("interviewer", "").strip()
        interview_date = form.get("interview_date", "").strip()
        if not transcript or not company:
            return HTMLResponse('<div class="text-red-400 text-sm">Transcript and Company are required.</div>')
        result = coach_interview(transcript, company, job_title, interviewer, interview_date)
        parts = ["<div class='space-y-4'>"]

        if result.get("per_response"):
            parts.append('<div class="text-xs font-semibold text-gray-400 mb-3">Per-Answer Analysis</div>')
            for resp in result["per_response"][:8]:
                parts.append('<div class="border border-gray-700 rounded p-3 mb-2">')
                if resp.get("question_asked"):
                    safe_q = _html.escape(str(resp["question_asked"]))
                    parts.append(f'<div class="text-xs text-gray-500 mb-2">Q: {safe_q}</div>')
                scores = resp.get("scores", {})
                for score_key in ["overall", "content", "structure", "delivery"]:
                    score_val = float(scores.get(score_key, 0))
                    bar_pct = int((score_val / 10) * 100)
                    color = "bg-green-600" if score_val >= 8 else "bg-yellow-600" if score_val >= 6 else "bg-red-600"
                    parts.append(f'<div class="flex items-center gap-2 mb-1"><span class="text-xs text-gray-500 w-16">{score_key.capitalize()}</span><div class="flex-1 bg-gray-800 rounded h-1.5"><div class="{color} h-full rounded" style="width:{bar_pct}%"></div></div><span class="text-xs text-gray-400">{score_val}</span></div>')
                for label, field, color in [("Strengths", "did_well", "text-green-400"), ("Improve", "improve", "text-yellow-400")]:
                    items = resp.get(field, [])[:2]
                    if items:
                        parts.append(f'<div class="text-xs {color} mt-1">{label}:</div>')
                        for item in items:
                            safe_item = _html.escape(str(item))
                            parts.append(f'<div class="text-xs text-gray-400">• {safe_item}</div>')
                parts.append("</div>")

        if result.get("filler_words"):
            fw = result["filler_words"]
            parts.append('<div class="border border-gray-700 rounded p-3">')
            parts.append('<div class="text-xs font-semibold text-gray-400 mb-2">Filler Words</div>')
            if fw.get("filler_rate_pct") is not None:
                parts.append(f'<div class="text-sm text-gray-300">Rate: {float(fw["filler_rate_pct"]):.1f}%</div>')
            for word, count in list((fw.get("breakdown") or {}).items())[:5]:
                if count:
                    safe_word = _html.escape(str(word))
                    parts.append(f'<div class="text-xs text-gray-400">• {safe_word}: {int(count)}</div>')
            parts.append("</div>")

        if result.get("overall"):
            ov = result["overall"]
            parts.append('<div class="border border-gray-700 rounded p-3 bg-gray-800">')
            parts.append('<div class="text-xs font-semibold text-gray-400 mb-2">Overall</div>')
            for label, field, color in [("Strengths", "top_strengths", "text-green-400"), ("Improvements", "top_improvements", "text-yellow-400")]:
                items = ov.get(field, [])[:3]
                if items:
                    parts.append(f'<div class="text-xs {color} mt-1">{label}:</div>')
                    for item in items:
                        safe_item = _html.escape(str(item))
                        parts.append(f'<div class="text-xs text-gray-400">• {safe_item}</div>')
            if ov.get("interviewer_sentiment"):
                safe_sent = _html.escape(str(ov["interviewer_sentiment"]))
                parts.append(f'<div class="text-xs text-gray-500 mt-2">Interviewer sentiment: {safe_sent}</div>')
            parts.append("</div>")

        parts.append("</div>")
        return HTMLResponse("".join(parts))
    except Exception as e:
        safe_e = _html.escape(str(e))
        return HTMLResponse(f'<div class="text-red-400 text-sm">Error: {safe_e}</div>')


async def generate_mock_questions_post(request: Request):
    import re
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse('<div class="text-red-400 text-sm">Job not found.</div>')

    job = dict(row)
    company = job.get("company", "")
    job_title = job.get("job_title", "")

    profile = load_profile()
    with get_db() as conn:
        research_row = conn.execute(
            "SELECT result_json FROM research_cache WHERE company_name = ?", (company,)
        ).fetchone()
    research = json.loads(research_row["result_json"]) if research_row else {}

    anchor_stories = json.dumps(
        [{"title": s.get("title", ""), "summary": s.get("summary", ""), "best_for": s.get("best_for", "")}
         for s in profile.get("interview", {}).get("anchor_stories", [])],
        indent=2,
    )

    from app.scoring.prompts import MOCK_QUESTIONS_PROMPT
    prompt = MOCK_QUESTIONS_PROMPT.format(
        job_title=job_title,
        company=company,
        research_summary=json.dumps(research, indent=2),
        role_analysis=f"{job_title} at {company}",
        anchor_stories=anchor_stories,
    )

    llm = get_provider()
    raw = llm.generate(prompt)

    # Extract first line of each numbered entry (the question itself)
    questions = []
    for line in raw.splitlines():
        m = re.match(r"^\d+\.\s+(.+)$", line.strip())
        if m:
            questions.append(m.group(1).strip())

    from app.questions.bank import add_question
    for q in questions:
        add_question(job_id, q, "Strategic", "Any", "Medium", source="mock_questions")

    ok = append_questions_to_prep_doc(company, job_title, questions)
    if not ok:
        import logging
        logging.warning("append_questions_to_prep_doc failed for %s", company)

    safe_count = int(len(questions))
    html_msg = f'Generated {safe_count} mock questions — added to question bank'
    if not ok:
        html_msg += ' (doc write skipped — re-auth needed)'
    
    return HTMLResponse(f'<div class="text-green-400 text-sm">{html_msg}.</div>')


async def questions_list(request: Request):
    job_id = int(request.path_params["job_id"])
    priority = request.query_params.get("priority")
    from app.questions.bank import get_questions
    questions = get_questions(job_id, priority=priority)

    # Enrich questions with suggested story bullets
    from app.scoring.corpus import load_corpus, get_bullets_for_themes
    import json as _json
    _corpus = load_corpus()
    for _q in questions:
        _raw = _q.get("suggested_themes")
        _themes = _json.loads(_raw) if _raw else []
        _q["suggested_bullets"] = get_bullets_for_themes(_themes, _corpus) if _themes else []

    jinja_tmpl = jinja.get_template("components/question_row.html")
    html = "".join(jinja_tmpl.render(q=q) for q in questions) or '<div class="text-sm text-gray-600">No questions.</div>'
    return HTMLResponse(html)


async def add_question_post(request: Request):
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    from app.questions.bank import add_question
    qid = add_question(job_id, form.get("question",""), form.get("category","Strategic"),
                       form.get("persona_target","Any"), form.get("priority","Medium"))
    with get_db() as conn:
        q = dict(conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone())
    return HTMLResponse(jinja.get_template("components/question_row.html").render(q=q))


async def mark_question_asked(request: Request):
    _job_id = int(request.path_params["job_id"])
    q_id = int(request.path_params["q_id"])
    from app.questions.bank import mark_asked
    mark_asked(q_id, "Unknown")
    with get_db() as conn:
        q = dict(conn.execute("SELECT * FROM questions WHERE id = ?", (q_id,)).fetchone())
    return HTMLResponse(jinja.get_template("components/question_row.html").render(q=q))


async def mark_question_answered(request: Request):
    _job_id = int(request.path_params["job_id"])
    q_id = int(request.path_params["q_id"])
    form = await request.form()
    from app.questions.bank import mark_answered
    mark_answered(q_id, form.get("answer_notes",""))
    with get_db() as conn:
        q = dict(conn.execute("SELECT * FROM questions WHERE id = ?", (q_id,)).fetchone())
    return HTMLResponse(jinja.get_template("components/question_row.html").render(q=q))


# ---------------------------------------------------------------------------
# Recruiters
# ---------------------------------------------------------------------------

async def recruiters_list(request: Request):
    tab = request.query_params.get("tab", "all")
    all_recruiters = get_all_recruiters()
    stale = get_stale_recruiters()
    stale_ids = {s["id"] for s in stale}

    def is_active(r): return r.get("relationship_status") == "Active"
    def is_warm(r):   return r.get("relationship_status") == "Warm"
    def is_cold(r):   return r.get("relationship_status") not in ("Active", "Warm")
    def is_stale(r):  return r["id"] in stale_ids

    counts = {
        "all":    len(all_recruiters),
        "active": sum(1 for r in all_recruiters if is_active(r)),
        "warm":   sum(1 for r in all_recruiters if is_warm(r)),
        "cold":   sum(1 for r in all_recruiters if is_cold(r)),
        "stale":  len(stale),
    }

    if tab == "active":
        recruiters = [r for r in all_recruiters if is_active(r)]
    elif tab == "warm":
        recruiters = [r for r in all_recruiters if is_warm(r)]
    elif tab == "cold":
        recruiters = [r for r in all_recruiters if is_cold(r)]
    elif tab == "stale":
        recruiters = [r for r in all_recruiters if is_stale(r)]
    else:
        recruiters = all_recruiters

    return render("recruiters.html", request=request, recruiters=recruiters, counts=counts, tab=tab)


async def recruiter_detail(request: Request):
    r_id = int(request.path_params["r_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM recruiters WHERE id = ?", (r_id,)).fetchone()
    if not row:
        return HTMLResponse('<div class="dim" style="padding:24px;">Recruiter not found.</div>')
    r = dict(row)
    # Mirror the days_since enrichment that get_all_recruiters performs.
    if r.get("last_contact_date"):
        try:
            from datetime import datetime as _dt
            r["days_since"] = (_dt.now() - _dt.fromisoformat(r["last_contact_date"])).days
        except Exception:
            r["days_since"] = None
    return HTMLResponse(jinja.get_template("components/recruiter_detail_panel.html").render(r=r))


async def recruiters_add(request: Request):
    form = await request.form()
    add_recruiter(
        name=form.get("name",""), firm=form.get("firm",""),
        linkedin_url=form.get("linkedin_url",""), email=form.get("email",""),
        phone=form.get("phone",""), specialty=form.get("specialty",""),
        notes=form.get("notes",""), relationship_status=form.get("relationship_status","Cold"),
    )
    # New layout swaps the full body; redirect back so the list refreshes.
    return RedirectResponse(url="/recruiters", status_code=303)


async def recruiters_log_contact(request: Request):
    r_id = int(request.path_params["r_id"])
    form = await request.form()
    log_contact(r_id, note=form.get("note",""))
    with get_db() as conn:
        r = dict(conn.execute("SELECT * FROM recruiters WHERE id = ?", (r_id,)).fetchone())
    if r.get("last_contact_date"):
        try:
            from datetime import datetime as _dt
            r["days_since"] = (_dt.now() - _dt.fromisoformat(r["last_contact_date"])).days
        except Exception:
            r["days_since"] = None
    return HTMLResponse(jinja.get_template("components/recruiter_detail_panel.html").render(r=r))


# ---------------------------------------------------------------------------
# Vetting / Rejected
# ---------------------------------------------------------------------------

async def vetting_view(request: Request):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE ethics_vetted = 0 AND auto_rejected = 0 AND final_score IS NOT NULL ORDER BY final_score DESC"
        ).fetchall()
    jobs = [_enrich_job(dict(r)) for r in rows]
    ethics_reasons = load_ethics_reasons(enabled_only=True)
    return render("vetting.html", request=request, jobs=jobs, stages=STAGES,
                  ethics_reasons=ethics_reasons)


async def rejected_view(request: Request):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE auto_rejected = 1 ORDER BY created_at DESC"
        ).fetchall()
    jobs = [_enrich_job(dict(r)) for r in rows]
    return render("rejected.html", request=request, jobs=jobs)


# ---------------------------------------------------------------------------
# Follow-ups widget
# ---------------------------------------------------------------------------

async def followup_log(request: Request):
    """Log a follow-up. Resets the nudge clock. Returns the updated row or empty on clock-reset."""
    job_id = int(request.path_params["job_id"])
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO followups (job_id, sent_at) VALUES (?, ?)", (job_id, now))
        except Exception:
            pass
        followups_due = get_followups_due(conn)
    f = next((x for x in followups_due if x["id"] == job_id), None)
    if f is None:
        return HTMLResponse("", headers={"HX-Trigger": "followups:row-removed"})
    return render("components/_followup_row.html", request=request, f=f)


async def followup_heard_back(request: Request):
    """Response received — remove the row and advance to screening."""
    job_id = int(request.path_params["job_id"])
    advance_stage(job_id, "screening", notes="Heard back — advanced from follow-up widget.")
    return HTMLResponse("", headers={"HX-Trigger": "followups:row-removed"})


async def followup_snooze(request: Request):
    """Defer the nudge without logging a follow-up."""
    job_id = int(request.path_params["job_id"])
    days = int(request.query_params.get("days", 3))
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET followup_snooze_until = ? WHERE id = ?", (until, job_id))
    return HTMLResponse("", headers={"HX-Trigger": "followups:row-removed"})


async def followup_close(request: Request):
    """Cold close — move to they_declined after 3+ unanswered follow-ups."""
    job_id = int(request.path_params["job_id"])
    advance_stage(job_id, "they_declined", decline_reason="No response (ghosted)")
    return HTMLResponse("", headers={"HX-Trigger": "followups:row-removed"})


async def followups_snooze_all(request: Request):
    """Snooze every currently-due item. Returns empty widget partial (collapses)."""
    days = int(request.query_params.get("days", 3))
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with get_db() as conn:
        due = get_followups_due(conn)
        for f in due:
            conn.execute("UPDATE jobs SET followup_snooze_until = ? WHERE id = ?", (until, f["id"]))
    return render("components/followups_due.html", request=request, followups_due=[])


async def followups_widget(request: Request):
    """Re-render the widget partial (called after row removal to refresh head count)."""
    with get_db() as conn:
        followups_due = get_followups_due(conn)
    return render("components/followups_due.html", request=request, followups_due=followups_due)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def settings_view(request: Request):
    from app.scoring.corpus import INVENTORY_PATH
    from app.config import LLM_PROVIDER
    from app import voice
    import hashlib
    import os
    profile = load_profile()
    inventory_sha = None
    if INVENTORY_PATH.exists():
        inventory_sha = hashlib.sha256(INVENTORY_PATH.read_bytes()).hexdigest()[:16] + "…"
    return render("settings.html",
                  request=request,
                  profile=profile,
                  slack_ok=bool(os.environ.get("SLACK_WEBHOOK_URL")),
                  gemini_ok=bool(os.environ.get("GEMINI_API_KEY")),
                  llm_provider=LLM_PROVIDER,
                  voice_enabled=voice.is_enabled(),
                  voice_guide=voice.active_guide_label(),
                  high_score_threshold=HIGH_SCORE_THRESHOLD,
                  inventory_sha=inventory_sha)


async def settings_rubric(request: Request):
    return render("settings_rubric.html", request=request)


async def settings_methodology(request: Request):
    return render("settings_methodology.html", request=request)


def _render_filter_list() -> HTMLResponse:
    """Return the filter list HTML fragment used by all filter CRUD routes."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, filter_type, value, enabled FROM title_filters ORDER BY filter_type, value"
        ).fetchall()
    positives = [dict(r) for r in rows if r["filter_type"] == "positive"]
    negatives = [dict(r) for r in rows if r["filter_type"] == "negative"]
    return render("settings_filters_partial.html", positives=positives, negatives=negatives)


async def settings_filters_list(request: Request):
    return _render_filter_list()


async def settings_filters_add(request: Request):
    form = await request.form()
    filter_type = form.get("filter_type", "").strip()
    value = form.get("value", "").strip()
    if filter_type in ("positive", "negative") and value:
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO title_filters (filter_type, value) VALUES (?,?)",
                    (filter_type, value),
                )
            except Exception as e:
                logging.error("Filter add failed: %s", e)
    return _render_filter_list()


async def settings_filters_toggle(request: Request):
    f_id = int(request.path_params["f_id"])
    with get_db() as conn:
        row = conn.execute("SELECT enabled FROM title_filters WHERE id=?", (f_id,)).fetchone()
        if row:
            conn.execute("UPDATE title_filters SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, f_id))
    return _render_filter_list()


async def settings_filters_delete(request: Request):
    f_id = int(request.path_params["f_id"])
    with get_db() as conn:
        conn.execute("DELETE FROM title_filters WHERE id=?", (f_id,))
    return _render_filter_list()


# ---------------------------------------------------------------------------
# No-go industries (Layer 1 auto-reject) — DB-backed, edited in Settings
# ---------------------------------------------------------------------------

def _render_no_go_list() -> HTMLResponse:
    """Return the no-go-industry list HTML fragment, grouped by sector."""
    from app.scoring.engine import _seed_no_go_if_empty
    with get_db() as conn:
        _seed_no_go_if_empty(conn)
        rows = conn.execute(
            "SELECT id, sector, keyword, enabled FROM no_go_industries ORDER BY sector, keyword"
        ).fetchall()
    sectors: dict[str, list] = {}
    for r in rows:
        sectors.setdefault(r["sector"], []).append(dict(r))
    return render("settings_no_go_partial.html", sectors=sectors)


async def settings_no_go_list(request: Request):
    return _render_no_go_list()


async def settings_no_go_add(request: Request):
    form = await request.form()
    sector = form.get("sector", "").strip()
    keyword = form.get("keyword", "").strip().lower()
    if sector and keyword:
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO no_go_industries (sector, keyword) VALUES (?,?)",
                    (sector, keyword),
                )
            except Exception as e:
                logging.error("No-go add failed: %s", e)
    return _render_no_go_list()


async def settings_no_go_toggle(request: Request):
    n_id = int(request.path_params["n_id"])
    with get_db() as conn:
        row = conn.execute("SELECT enabled FROM no_go_industries WHERE id=?", (n_id,)).fetchone()
        if row:
            conn.execute("UPDATE no_go_industries SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, n_id))
    return _render_no_go_list()


async def settings_no_go_delete(request: Request):
    n_id = int(request.path_params["n_id"])
    with get_db() as conn:
        conn.execute("DELETE FROM no_go_industries WHERE id=?", (n_id,))
    return _render_no_go_list()


# ---------------------------------------------------------------------------
# Ethics reasons — reference checklist for manual vetting; DB-backed
# ---------------------------------------------------------------------------

DEFAULT_ETHICS_REASONS = [
    "Predatory or exploitative business model",
    "Significant environmental harm",
    "Weapons / defense manufacturing",
    "Misleading or deceptive marketing",
    "Poor labor practices or worker exploitation",
    "Surveillance or privacy-violating products",
    "Investors or funding sources with ethical concerns",
]


def _seed_ethics_reasons_if_empty(conn) -> None:
    """Populate ethics_reasons from DEFAULT_ETHICS_REASONS if the table is empty."""
    count = conn.execute("SELECT COUNT(*) FROM ethics_reasons").fetchone()[0]
    if count > 0:
        return
    for reason in DEFAULT_ETHICS_REASONS:
        try:
            conn.execute("INSERT OR IGNORE INTO ethics_reasons (reason) VALUES (?)", (reason,))
        except Exception:
            pass


def load_ethics_reasons(enabled_only: bool = False) -> list[dict]:
    """Ethics reasons from the DB (seeds defaults on first run). Returns list of dicts.
    Falls back to DEFAULT_ETHICS_REASONS if the DB is unavailable."""
    try:
        with get_db() as conn:
            _seed_ethics_reasons_if_empty(conn)
            sql = "SELECT id, reason, enabled FROM ethics_reasons"
            if enabled_only:
                sql += " WHERE enabled=1"
            sql += " ORDER BY reason"
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logging.warning("Could not load ethics_reasons: %s", e)
        return [{"id": 0, "reason": r, "enabled": 1} for r in DEFAULT_ETHICS_REASONS]


def _render_ethics_list() -> HTMLResponse:
    """Return the ethics-reason list HTML fragment used by all ethics CRUD routes."""
    return render("settings_ethics_partial.html", reasons=load_ethics_reasons())


async def settings_ethics_list(request: Request):
    return _render_ethics_list()


async def settings_ethics_add(request: Request):
    form = await request.form()
    reason = form.get("reason", "").strip()
    if reason:
        with get_db() as conn:
            try:
                conn.execute("INSERT OR IGNORE INTO ethics_reasons (reason) VALUES (?)", (reason,))
            except Exception as e:
                logging.error("Ethics reason add failed: %s", e)
    return _render_ethics_list()


async def settings_ethics_toggle(request: Request):
    e_id = int(request.path_params["e_id"])
    with get_db() as conn:
        row = conn.execute("SELECT enabled FROM ethics_reasons WHERE id=?", (e_id,)).fetchone()
        if row:
            conn.execute("UPDATE ethics_reasons SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, e_id))
    return _render_ethics_list()


async def settings_ethics_delete(request: Request):
    e_id = int(request.path_params["e_id"])
    with get_db() as conn:
        conn.execute("DELETE FROM ethics_reasons WHERE id=?", (e_id,))
    return _render_ethics_list()


async def settings_profile_save(request: Request):
    from app.config import load_profile, save_profile
    form = await request.form()
    profile = load_profile()
    try:
        comp_floor = float(form.get("comp_floor", ""))
        profile.setdefault("compensation", {})["base_min"] = comp_floor
    except (ValueError, TypeError):
        pass
    target_range = form.get("target_range", "").strip()
    if target_range:
        profile.setdefault("compensation", {})["target_range"] = target_range
    try:
        base_score = float(form.get("base_score", ""))
        profile.setdefault("scoring", {})["base_score"] = base_score
    except (ValueError, TypeError):
        pass
    save_profile(profile)
    return HTMLResponse('<span style="color:var(--accent);font-size:12px;">Saved ✓</span>')


async def guide_view(request: Request):
    return render("guide.html", request=request)


async def settings_test_slack(request: Request):
    from app.notifications.slack import test_webhook
    ok = test_webhook()
    return HTMLResponse("Sent!" if ok else "Failed — check SLACK_WEBHOOK_URL")


async def settings_send_digest(request: Request):
    from app.notifications.slack import send_weekly_digest
    ok = send_weekly_digest()
    return HTMLResponse("Digest sent!" if ok else "Failed — check SLACK_WEBHOOK_URL")


_SYNC_POLLING_FRAGMENT = (
    '<div id="sync-status"'
    ' hx-get="/api/sync-status"'
    ' hx-trigger="every 5s"'
    ' hx-target="#sync-status"'
    ' hx-swap="outerHTML"'
    ' style="font-size:13px;">'
    '⏳ Sync running&hellip; checking every 5s'
    '</div>'
)


async def manual_sync(request: Request):
    return HTMLResponse('<span style="color:var(--tier-pass);">⚠️ Google Sheets sync removed. Add jobs via the Score a Job form.</span>')


async def api_sync_status(request: Request):
    """Return sync status fragment; keeps hx-trigger only while running."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT status, message, logged_at FROM task_log"
                " WHERE task_type = 'sheet_sync' ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception as e:
        logging.warning(f"[sync-status] DB error: {e}")
        return HTMLResponse(_SYNC_POLLING_FRAGMENT)

    if not row:
        return HTMLResponse(_SYNC_POLLING_FRAGMENT)

    status = row["status"]
    if status == "started":
        return HTMLResponse(_SYNC_POLLING_FRAGMENT)

    ts = (row["logged_at"] or "")[:16]
    msg = _html.escape(row["message"] or "")
    if status == "completed":
        return HTMLResponse(
            f'<div id="sync-status" style="font-size:13px;">'
            f'✅ Done at {ts} — {msg}. Check the <strong>New</strong> tab in '
            f'<a href="/discovered" style="text-decoration:underline;">Discovered →</a> '
            f'for any newly scored jobs.'
            f'</div>'
        )
    return HTMLResponse(
        f'<div id="sync-status" style="font-size:13px;color:var(--tier-pass);">'
        f'❌ Sync failed at {ts}: {msg}'
        f'</div>'
    )


async def fix_sheet_headers(request: Request):
    return HTMLResponse("Google Sheets removed.")


async def backfill_question_themes(request: Request):
    """Backfill story anchor tags for questions missing suggested_themes."""
    from app.questions.bank import infer_themes
    import json as _json
    with get_db() as conn:
        qs = conn.execute(
            "SELECT id, question, category FROM questions WHERE suggested_themes IS NULL"
        ).fetchall()
        for q in qs:
            themes = _json.dumps(infer_themes(q["question"], q["category"]))
            conn.execute(
                "UPDATE questions SET suggested_themes=? WHERE id=?",
                (themes, q["id"]),
            )
    count = len(qs)
    return HTMLResponse(f'<span class="dim">Tagged {count} question{"s" if count != 1 else ""}.</span>')


# ---------------------------------------------------------------------------
# Admin Analytics
# ---------------------------------------------------------------------------

async def admin_patterns_get(request: Request):
    """Analyze which signals correlate with screening calls."""
    return render("admin_patterns.html",
                  request=request,
                  calibration=compute_calibration(),
                  **compute_patterns())


async def admin_rescore_get(request: Request):
    """Show count of jobs needing rescore."""
    with get_db() as conn:
        count_result = conn.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE match_score IS NULL AND jd_text IS NOT NULL AND auto_rejected = 0"
        ).fetchone()
    count = count_result['cnt'] if count_result else 0
    return render("admin_rescore.html", request=request, count=count)


async def admin_rescore_post(request: Request):
    """Batch rescore all jobs with match_score IS NULL."""
    import time
    from app.scoring.research import score_job

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, jd_text FROM jobs WHERE match_score IS NULL AND jd_text IS NOT NULL AND auto_rejected = 0"
        ).fetchall()

    results = {"ok": 0, "skip": 0, "errors": 0}

    for row in rows:
        try:
            rec = score_job(row["jd_text"])
            if rec.get("jd_insufficient"):
                results["skip"] += 1
                continue

            # Use the canonical persistence path so auto_rejected / reject_reason /
            # sector and all metadata fields stay in sync (audit fix 2026-06-11).
            persist_score_record_to_job(row["id"], rec, jd_text=row["jd_text"])
            results["ok"] += 1
        except Exception as e:
            results["errors"] += 1
            logging.error(f"[rescore] error on job {row['id']}: {e}")

        time.sleep(5)  # AI_RULES §1: 5s pacing between LLM calls

    mins = (results["ok"] * 5) // 60
    secs = (results["ok"] * 5) % 60
    return HTMLResponse(f'<div class="dim" style="padding:1rem">✅ Rescore complete: {results["ok"]} updated, {results["skip"]} skipped (insufficient JD), {results["errors"]} errors. Processing time: ~{mins}m {secs}s</div>')


# ---------------------------------------------------------------------------
# Job Discovery
# ---------------------------------------------------------------------------

async def discovered_view(request: Request):
    tab = request.query_params.get("tab", "new")
    source_filter = request.query_params.get("source", "all")
    sort_col   = request.query_params.get("sort", "")
    sort_order = request.query_params.get("order", "asc").lower()
    _DISC_ALLOWED = {"job_title", "discovery_source", "date_added",
                     "salary_range_detected", "final_score", "pipeline_stage"}
    if sort_col not in _DISC_ALLOWED:
        sort_col = ""

    # Stages that mean the job has moved into active Pipeline work — exclude from Discovered
    _PIPELINE_ACTIVE = (
        'outreach', 'recruiter', 'hm_interview', 'panel', 'final_offer',
        'accepted', 'they_declined', 'on_hold',
    )
    _PIPELINE_ACTIVE_SQL = ','.join(f"'{s}'" for s in _PIPELINE_ACTIVE)

    with get_db() as conn:
        all_rows = conn.execute(
            f"""SELECT * FROM jobs
                WHERE pipeline_stage != 'duplicate'
                  AND pipeline_stage NOT IN ({_PIPELINE_ACTIVE_SQL})
                  AND (COALESCE(discovery_source,'') LIKE 'hunt%'
                   OR discovery_source = 'manual'
                   OR discovery_source = 'linkedin'
                   OR pipeline_stage = 'discovered')
                ORDER BY COALESCE(date_added, date_found) DESC"""
        ).fetchall()
    all_jobs = [_enrich_job(dict(r)) for r in all_rows]

    # Apply source filter
    if source_filter == "manual":
        all_jobs = [j for j in all_jobs if j.get("discovery_source") == "manual"]
    elif source_filter == "linkedin":
        all_jobs = [j for j in all_jobs if j.get("discovery_source") == "linkedin"]
    elif source_filter == "scan":
        all_jobs = [j for j in all_jobs if j.get("discovery_source") not in ("manual", "linkedin", None, "")]

    def is_new(j):       return j.get("pipeline_stage") in ("discovered", "identified")
    def is_dismissed(j): return j.get("pipeline_stage") in ("i_declined", "job_listing_closed", "duplicate")
    def is_promoted(j):  return not is_new(j) and not is_dismissed(j)

    counts = {
        "new":       sum(1 for j in all_jobs if is_new(j)),
        "promoted":  sum(1 for j in all_jobs if is_promoted(j)),
        "dismissed": sum(1 for j in all_jobs if is_dismissed(j)),
        "all":       len(all_jobs),
    }

    if tab == "promoted":
        jobs = [j for j in all_jobs if is_promoted(j)]
    elif tab == "dismissed":
        jobs = [j for j in all_jobs if is_dismissed(j)]
    elif tab == "all":
        jobs = all_jobs
    else:
        jobs = [j for j in all_jobs if is_new(j)]

    if sort_col:
        _has = [j for j in jobs if j.get(sort_col) not in (None, "")]
        _nil  = [j for j in jobs if j.get(sort_col) in (None, "")]
        _has.sort(
            key=lambda j: j[sort_col] if isinstance(j.get(sort_col), (int, float))
                          else str(j.get(sort_col, "")).lower(),
            reverse=(sort_order == "desc"),
        )
        jobs = _has + _nil

    return render(
        "discovered.html",
        request=request,
        jobs=jobs,
        counts=counts,
        tab=tab,
        source_filter=source_filter,
        sort=sort_col,
        order=sort_order,
        discovered_count=counts["new"],
    )


async def discovered_detail(request: Request):
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse('<div class="dim" style="padding:24px;">Job not found.</div>')
    job = _enrich_job(dict(row))
    fit_bullets: list = []
    raw = job.get("fit_bullets")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                fit_bullets = parsed
        except Exception:
            pass
    elif isinstance(raw, list):
        fit_bullets = raw
    return HTMLResponse(jinja.get_template("components/discovered_detail_panel.html").render(job=job, fit_bullets=fit_bullets))


async def discovered_row(request: Request):
    """Render just the <tr> for a single discovered job — used by the
    jobScored client listener to refresh the row in place without a page reload."""
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse("", status_code=204)
    job = dict(row)
    return HTMLResponse(
        jinja.get_template("components/_discovered_row.html").render(j=job)
    )


async def job_promote_from_discovery(request: Request):
    job_id = int(request.path_params["job_id"])
    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return HTMLResponse('<div class="text-red-400 text-sm">Job not found</div>')
        job = dict(job)

    # Move to identified stage
    from app.pipeline.tracker import advance_stage
    result = advance_stage(job_id, 'identified', notes='Promoted from discovery')
    if not result["ok"]:
        safe_err = _html.escape(result["error"])
        return HTMLResponse(f'<div class="text-red-400 text-sm">{safe_err}</div>')

    # Trigger scoring via the canonical service path (persists the full score
    # record — evidence, mismatches, bullets, hooks — and handles the alert).
    # Stage was already advanced above, so no transition_stage here.
    score_result = {"status": "skipped"}
    if job.get('url'):
        score_result = score_job_from_url_and_persist(job_id, job['url'])
    if score_result.get("status") != "success" and (job.get('jd_text') or '').strip():
        score_result = score_job_from_text_and_persist(job_id, job['jd_text'])
    if score_result.get("status") not in ("success", "skipped"):
        logging.error("Scoring failed during promotion of job %s: %s",
                      job_id, score_result.get("error"))

    return RedirectResponse(url=f"/job/{job_id}", status_code=302)


async def job_dismiss_from_discovery(request: Request):
    job_id = int(request.path_params["job_id"])

    # Move to i_declined stage
    from app.pipeline.tracker import advance_stage
    result = advance_stage(job_id, 'i_declined', decline_reason='dismissed_from_discovery')
    if not result["ok"]:
        safe_err = _html.escape(result["error"])
        return HTMLResponse(f'<div class="text-red-400 text-sm">{safe_err}</div>')

    return RedirectResponse(url="/discovered", status_code=302)


async def job_stage_update(request: Request):
    """Update pipeline stage from the detail panel dropdown and return the refreshed panel."""
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    result = update_job_stage(job_id, (form.get("stage") or "").strip())

    if result["status"] == "invalid_stage":
        return HTMLResponse('<div class="dim" style="padding:24px;">Invalid stage.</div>', status_code=400)
    if result["status"] == "not_found":
        return HTMLResponse('<div class="dim" style="padding:24px;">Job not found.</div>')

    job = _enrich_job(result["job"])
    fit_bullets = json.loads(job.get("match_bullets_json") or "[]")
    html = jinja.get_template("components/discovered_detail_panel.html").render(job=job, fit_bullets=fit_bullets)
    is_promoted = result["promoted"]
    stage_label = _html.escape(result["stage_label"])
    remove_row_js = f"var row=document.querySelector('tr[data-job-id=\"{job_id}\"]');if(row)row.remove();" if is_promoted else ""
    toast_msg = f"Moved to {stage_label} — now in Pipeline" if is_promoted else "Stage saved"
    html += f"""<script>
(function(){{
  var sel = document.querySelector('#discovered-panel select[name="stage"]');
  if(sel){{
    sel.style.transition='none';
    sel.style.borderColor='oklch(65% 0.18 145)';
    sel.style.backgroundColor='oklch(30% 0.06 145 / 0.25)';
    setTimeout(function(){{
      sel.style.transition='border-color 600ms ease-out, background-color 600ms ease-out';
      sel.style.borderColor='';
      sel.style.backgroundColor='';
    }}, 50);
  }}
  {remove_row_js}
  if(window.showToast) window.showToast('{toast_msg}');
}})();
</script>"""
    return HTMLResponse(html)


async def job_close_listing(request: Request):
    job_id = int(request.path_params["job_id"])
    result = advance_stage(job_id, 'job_listing_closed', decline_reason='Listing removed')
    if not result["ok"]:
        safe_err = _html.escape(result["error"])
        return HTMLResponse(f'<span class="dim" style="font-size:12px;">{safe_err}</span>', status_code=400)
    return HTMLResponse("", status_code=204)


async def job_fetch_and_score(request: Request):
    """Fetch JD and run full scoring for a stub job (pipeline_stage='identified', no score)."""
    job_id = int(request.path_params["job_id"])
    result = fetch_and_score_stub(job_id)

    if result["status"] == "not_found":
        return HTMLResponse('<div class="dim" style="padding:24px;">Job not found.</div>')
    if result["status"] == "no_url":
        return HTMLResponse('<div class="dim" style="padding:24px;">No URL stored for this job — cannot fetch.</div>')
    if result["status"] == "linkedin_blocked":
        return HTMLResponse(
            '<div style="padding:12px;font-size:13px;color:var(--tier-pass);">'
            '❌ LinkedIn blocks automated fetching. Use the "Paste JD text" form below.'
            '</div>'
        )
    if result["status"] == "error":
        return HTMLResponse(
            f'<div style="padding:20px;font-size:13px;color:var(--tier-pass);">'
            f'❌ Could not fetch JD from this URL (attempt {result["attempts"]}/3). '
            f'Try pasting the JD text into '
            f'<a href="/" style="text-decoration:underline;">Dashboard → Score a Job</a>.'
            f'</div>'
        )

    fs = result.get("score")
    score_display = f" — score: {fs:.1f}" if fs else ""
    return HTMLResponse(
        f'<div class="just-scored" style="padding:12px;font-size:13px;">'
        f'<span aria-hidden="true">✅</span> Scored{score_display}'
        f'</div>',
        headers={"HX-Trigger": json.dumps({"jobScored": {"job_id": job_id, "score": fs}})},
    )


async def job_paste_and_score(request: Request):
    """Score a stub job from pasted JD text (bypasses automated fetch)."""
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    jd_text = (form.get("jd_text") or "").strip()

    if not jd_text:
        return HTMLResponse('<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ No JD text provided.</div>')
    if len(jd_text) < 100:
        return HTMLResponse('<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ JD text too short — paste the full job description.</div>')

    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse('<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ Job not found.</div>')

    result = score_job_from_text_and_persist(job_id, jd_text, transition_stage=True)

    if result["status"] == "error":
        error_msg = result.get("error", "Unknown error")
        return HTMLResponse(
            f'<div style="padding:12px;font-size:13px;color:var(--tier-pass);">'
            f'❌ Scoring error: {_html.escape(error_msg)}'
            f'</div>'
        )

    fs = result.get("score")
    score_display = f" — score: {fs:.1f}" if fs else ""
    return HTMLResponse(
        f'<div class="just-scored" style="padding:12px;font-size:13px;">'
        f'<span aria-hidden="true">✅</span> Scored from paste{score_display}'
        f'</div>',
        headers={"HX-Trigger": json.dumps({"jobScored": {"job_id": job_id, "score": fs}})},
    )


async def job_rescore(request: Request):
    """Re-score a job using its existing jd_text — clears auto-reject flags first."""
    job_id = int(request.path_params["job_id"])

    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return HTMLResponse('<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ Job not found.</div>')

    job = dict(row)
    jd_text = (job.get("jd_text") or "").strip()
    if len(jd_text) < 100:
        return HTMLResponse('<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ No JD text in DB — paste the JD first.</div>')

    result = score_job_from_text_and_persist(job_id, jd_text, transition_stage=False)

    if result["status"] == "error":
        error_msg = result.get("error", "Unknown error")
        return HTMLResponse(
            f'<div style="padding:12px;font-size:13px;color:var(--tier-pass);">'
            f'❌ Scoring error: {_html.escape(error_msg)}'
            f'</div>'
        )

    fs = result.get("score")
    score_display = f" — score: {fs:.1f}" if fs else ""
    return HTMLResponse(
        f'<div class="just-scored" style="padding:12px;font-size:13px;">'
        f'<span aria-hidden="true">✅</span> Re-scored{score_display}'
        f'</div>',
        headers={"HX-Trigger": json.dumps({"jobScored": {"job_id": job_id, "score": fs}})},
    )


def _render_job_add_result(result: dict) -> HTMLResponse:
    """Map a handle_job_add_with_optional_scoring result dict to the HTMX fragment."""
    status = result["status"]
    if status == "duplicate":
        label = _html.escape(f"{result['company']} — {result['job_title']}")
        dest = "/discovered?tab=dismissed" if result.get("pipeline_stage") in ("i_declined", "they_declined", "job_listing_closed") else "/discovered"
        return HTMLResponse(
            f'<div style="padding:12px;font-size:13px;">'
            f'⚠️ Already in your pipeline: <a href="{dest}" style="text-decoration:underline;">{label} — view →</a>'
            f'</div>'
        )
    if status == "error":
        return HTMLResponse(
            f'<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ {_html.escape(result.get("error", "Unknown error"))}</div>'
        )
    if status == "declined_fast_path":
        co = _html.escape(result.get("company", "Job"))
        return HTMLResponse(
            f'<div style="padding:12px;font-size:13px;">'
            f'✓ {co} logged as declined — '
            f'<a href="/discovered?tab=dismissed" style="text-decoration:underline;">view in Dismissed →</a>'
            f'</div>'
        )
    if status == "added_no_fetch":
        return HTMLResponse(
            '<div style="padding:12px;font-size:13px;">'
            '⚠️ Added but couldn\'t fetch JD — '
            '<a href="/discovered" style="text-decoration:underline;">open in Discovered →</a> '
            'to paste the JD manually.'
            '</div>'
        )
    if status == "added_score_failed":
        return HTMLResponse(
            '<div style="padding:12px;font-size:13px;">'
            '⚠️ Added but scoring failed — '
            '<a href="/discovered" style="text-decoration:underline;">open in Discovered →</a> '
            'to retry.'
            '</div>'
        )
    if status == "scored":
        fs = result.get("score", 0)
        tier = "tier-dream" if fs >= 9 else "tier-solid" if fs >= 7 else "tier-worth" if fs >= 5 else "tier-skip" if fs >= 3 else "tier-pass"
        return HTMLResponse(
            f'<div style="padding:12px;font-size:13px;">'
            f'✅ Scored <span class="score-badge {tier}"><span class="dot"></span>{fs:.1f}</span> — '
            f'<a href="/discovered" style="text-decoration:underline;">view in Discovered →</a>'
            f'</div>'
        )
    return HTMLResponse('<div style="padding:12px;font-size:13px;">Unknown result status</div>')


async def linkedin_add(request: Request):
    """Accept any job URL + optional title/company, dedup, insert, fetch & score.
    Works with LinkedIn, Greenhouse, Lever, Ashby, and generic company careers pages.
    Pass action=declined to skip scoring and log immediately as they_declined.
    """
    form = await request.form()
    url = (form.get("url") or "").strip()
    job_title = (form.get("job_title") or "").strip()
    company = (form.get("company") or "").strip()
    skip_score = (form.get("action") or "").strip() == "declined"

    if not url:
        return HTMLResponse('<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ URL is required.</div>')

    try:
        from app.security.url_guard import validate_url
        url = validate_url(url)
    except ValueError as e:
        return HTMLResponse(f'<div style="padding:12px;font-size:13px;color:var(--tier-pass);">❌ Invalid URL: {_html.escape(str(e))}</div>')

    result = handle_job_add_with_optional_scoring(url, job_title, company, skip_score)
    return _render_job_add_result(result)


async def admin_retry_stubs(request: Request):
    """Spawn a sync run that will retry eligible stubs (attempts < 3)."""
    with get_db() as conn:
        count = conn.execute(
            """SELECT COUNT(*) as c FROM jobs
               WHERE pipeline_stage = 'identified' AND final_score IS NULL
                 AND COALESCE(jd_fetch_attempts, 0) < 3"""
        ).fetchone()["c"]

    if count == 0:
        return HTMLResponse('<span class="dim" style="font-size:13px;">No retryable stubs — all have been attempted 3+ times or are already scored.</span>')

    from app.jobs.fetch import _retry_stubs, _reset_exhausted_stubs
    try:
        _reset_exhausted_stubs()
        _retry_stubs([])
        return HTMLResponse('<span style="font-size:13px;">✅ Retried stubs. <a href="/discovered" style="text-decoration:underline;">Check Discovered →</a></span>')
    except Exception as e:
        logging.error("admin_retry_stubs error: %s", e)
        return HTMLResponse(f'<span style="color:var(--tier-pass);font-size:13px;">❌ Retry failed: {_html.escape(str(e))}</span>')


async def admin_import_tier_a(request: Request):
    """One-time import of Tier A companies from the Google Sheet into the companies table.
    Safe to call multiple times — upserts by name.
    """
    result = import_tier_a_companies()

    if result["status"] == "fetch_error":
        return HTMLResponse(
            f'<span style="color:var(--tier-skip);font-size:13px;">Sheet fetch failed: {_html.escape(result["error"])}</span>'
        )
    if result["status"] == "db_error":
        return HTMLResponse(
            f'<span style="color:var(--tier-skip);font-size:13px;">DB error: {_html.escape(result["error"])}</span>'
        )

    return HTMLResponse(
        f'<span style="font-size:13px;color:var(--text);">'
        f'Done — {result["inserted"]} inserted, {result["updated"]} updated. '
        f'<a href="/targets" style="text-decoration:underline;">View Hunt Targets →</a>'
        f'</span>'
    )


async def targets_list(request: Request):
    tab = request.query_params.get("tab", "tier_a")
    sort_col   = request.query_params.get("sort", "")
    sort_order = request.query_params.get("order", "asc").lower()
    _TGT_ALLOWED = {"name", "industry_category", "remote_friendly",
                    "nearest_hq", "funding_stage", "gap_hypothesis", "match_count"}
    if sort_col not in _TGT_ALLOWED:
        sort_col = ""

    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.*
                 FROM companies c
                WHERE c.tier_a = 1
                   OR (c.careers_url IS NOT NULL AND c.careers_url <> '')
                ORDER BY c.tier_a DESC, c.last_scanned DESC NULLS LAST, c.name"""
        ).fetchall()
    all_rows = [dict(r) for r in rows]

    counts = {
        "all":        len(all_rows),
        "tier_a":     sum(1 for r in all_rows if r.get("tier_a")),
        "researched": sum(1 for r in all_rows if r.get("tier_a") and r.get("gap_hypothesis")),
        "matches":    sum(1 for r in all_rows if (r.get("match_count") or 0) > 0 and r.get("hunt_enabled")),
        "monitoring": sum(1 for r in all_rows if r.get("hunt_enabled") and not (r.get("match_count") or 0) > 0),
    }

    if tab == "tier_a":
        targets = [r for r in all_rows if r.get("tier_a")]
    elif tab == "researched":
        targets = [r for r in all_rows if r.get("tier_a") and r.get("gap_hypothesis")]
    elif tab == "matches":
        targets = [r for r in all_rows if (r.get("match_count") or 0) > 0 and r.get("hunt_enabled")]
    elif tab == "monitoring":
        targets = [r for r in all_rows if r.get("hunt_enabled")]
    else:
        targets = all_rows

    if sort_col:
        _has = [t for t in targets if t.get(sort_col) not in (None, "")]
        _nil  = [t for t in targets if t.get(sort_col) in (None, "")]
        _has.sort(
            key=lambda t: t[sort_col] if isinstance(t.get(sort_col), (int, float))
                          else str(t.get(sort_col, "")).lower(),
            reverse=(sort_order == "desc"),
        )
        targets = _has + _nil

    return render("targets.html", request=request, targets=targets, counts=counts,
                  tab=tab, sort=sort_col, order=sort_order)


async def target_detail(request: Request):
    co_id = int(request.path_params["co_id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
        if not row:
            return HTMLResponse('<div class="dim" style="padding:24px;">Target not found.</div>')
        target = dict(row)
        match_rows = conn.execute(
            """SELECT id, job_title, date_added, date_found, pipeline_stage,
                      COALESCE(final_score, lightweight_score) AS match_score
                 FROM jobs
                WHERE company_id = ?
                  AND COALESCE(discovery_source,'') LIKE 'hunt%'
                ORDER BY match_score DESC NULLS LAST, COALESCE(date_added, date_found) DESC
                LIMIT 20""",
            (co_id,)
        ).fetchall()
    matches = [dict(r) for r in match_rows]
    if target.get("research_json"):
        try:
            r = json.loads(target["research_json"])
            target["fit_rationale"] = r.get("fit_rationale")
        except Exception:
            pass
    return HTMLResponse(jinja.get_template("components/target_detail_panel.html").render(target=target, matches=matches))


async def targets_add(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    careers_url = form.get("careers_url", "").strip()

    if not name or not careers_url:
        return RedirectResponse(url="/targets", status_code=302)

    from app.discovery.ats_clients import detect_ats
    ats_type, ats_handle = detect_ats(careers_url)

    with get_db() as conn:
        existing = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
        if existing:
            co_id = existing['id']
            conn.execute(
                "UPDATE companies SET careers_url=?, ats_type=?, ats_handle=?, hunt_enabled=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (careers_url, ats_type, ats_handle, co_id)
            )
        else:
            conn.execute(
                "INSERT INTO companies (name, careers_url, ats_type, ats_handle, hunt_enabled, date_added, status) VALUES (?,?,?,?,?,?,?)",
                (name, careers_url, ats_type, ats_handle, 1, str(date.today()), 'Watchlist')
            )
            co_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    return RedirectResponse(url="/targets?added=1", status_code=302)


async def targets_toggle(request: Request):
    co_id = int(request.path_params["co_id"])
    with get_db() as conn:
        current = conn.execute("SELECT hunt_enabled FROM companies WHERE id = ?", (co_id,)).fetchone()
        if current:
            new_val = 0 if current['hunt_enabled'] else 1
            conn.execute("UPDATE companies SET hunt_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_val, co_id))
    return RedirectResponse(url="/targets", status_code=302)


def _render_scan_status(co_id: int, status: dict) -> HTMLResponse:
    """Render the Scan Status panel fragment for a hunt-target company."""
    last = (status["last_scanned"] or "")[:16] if status["last_scanned"] else "—"
    err = status["scan_error"]
    ats = status["ats_type"] or "—"
    handle = status["ats_handle"] or ""
    active = status["hunt_enabled"]

    monitoring_html = (
        '<span class="stage dim">Active</span>' if active
        else '<span class="stage" style="background:oklch(35% 0.05 40);color:oklch(75% 0.08 40);">Paused</span>'
    )
    error_html = (
        f'<dt class="dim">Error</dt><dd style="color:var(--tier-pass);font-size:12px;">{_html.escape(err[:120])}</dd>'
        if err else ""
    )
    return HTMLResponse(
        f'<section class="info-section" id="scan-status-{co_id}">'
        f'  <div class="info-section-head">Scan Status</div>'
        f'  <div class="info-section-body">'
        f'    <dl class="dl" style="display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:13px;margin:0;">'
        f'      <dt class="dim">ATS</dt><dd class="mono" style="font-size:12px;">{_html.escape(ats)}{"&nbsp;·&nbsp;" + _html.escape(handle) if handle else ""}</dd>'
        f'      <dt class="dim">Last scan</dt><dd class="mono" style="font-size:12px;">{_html.escape(last)}</dd>'
        f'      <dt class="dim">Monitoring</dt><dd>{monitoring_html}</dd>'
        f'      {error_html}'
        f'    </dl>'
        f'  </div>'
        f'</section>'
    )


async def targets_scan_now(request: Request):
    import asyncio
    co_id = int(request.path_params["co_id"])
    result = await asyncio.to_thread(scan_target_company, co_id)

    if result["status"] == "not_found":
        return HTMLResponse('<div class="dim" style="font-size:12px;">Company not found.</div>')
    if result["status"] == "error":
        return HTMLResponse(
            f'<div style="color:var(--tier-pass);font-size:12px;">Scan error: {_html.escape(result["error"][:120])}</div>'
        )
    return _render_scan_status(co_id, result["scan"])


async def targets_research(request: Request):
    """Run research pipeline for a Hunt Target company. Populates metadata + gap hypothesis."""
    import asyncio
    co_id = int(request.path_params["co_id"])

    with get_db() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
        if not row:
            return HTMLResponse('Company not found.', status_code=404)

    force_metadata = request.query_params.get("force") == "1"

    async def _run():
        from app.services.research_service import generate_gap_hypothesis
        try:
            generate_gap_hypothesis(co_id, force_research=True, force_metadata=force_metadata)
        except Exception as e:
            logging.error("Gap hypothesis task failed for company %s: %s", co_id, e)

    asyncio.create_task(_run())

    return HTMLResponse(
        f'Researching… panel will refresh automatically in ~70s.'
        f'<span hx-get="/targets/{co_id}/detail"'
        f'      hx-target="#targets-panel"'
        f'      hx-swap="innerHTML"'
        f'      hx-trigger="load delay:70s"></span>'
    )


def _render_match_cell(company: dict | None, refresh_url: str) -> HTMLResponse:
    """Render the match-summary cell fragment (count + best score badge + refresh button)."""
    if not company:
        return HTMLResponse('<span class="dim" style="font-size:11px;">—</span>')
    return HTMLResponse(
        jinja.get_template("components/match_cell.html").render(co=company, refresh_url=refresh_url)
    )


async def targets_refresh_matches(request: Request):
    import asyncio
    co_id = int(request.path_params["co_id"])
    company = await asyncio.to_thread(refresh_company_matches, co_id)
    return _render_match_cell(company, f"/targets/{co_id}/refresh-matches")


async def companies_refresh_matches(request: Request):
    import asyncio
    co_id = int(request.path_params["co_id"])
    company = await asyncio.to_thread(refresh_company_matches, co_id)
    return _render_match_cell(company, f"/companies/{co_id}/refresh-matches")


async def api_discovery_scan(request: Request):
    from app.discovery.hunter import run_discovery_scan
    stats = run_discovery_scan()
    return JSONResponse(stats)


async def api_task_status(request: Request):
    """Return last 20 task_log events as an HTMX HTML fragment for live polling."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT task_type, status, message, entity_name, logged_at FROM task_log ORDER BY id DESC LIMIT 20"
        ).fetchall()

    if not rows:
        return HTMLResponse('<div class="dim" style="font-size:12px;padding:8px 0;">No background tasks recorded yet.</div>')

    STATUS_ICON = {"started": "⏳", "completed": "✅", "failed": "❌", "partial": "⚠️"}
    tmpl = jinja.get_template("components/task_log_row.html")
    lines = []
    for r in rows:
        icon = STATUS_ICON.get(r["status"], "•")
        ts = (r["logged_at"] or "")[:16]
        name = _html.escape(r["entity_name"] or "")
        msg = _html.escape(r["message"] or "")
        lines.append(tmpl.render(icon=icon, ts=ts, label=name or msg))
    return HTMLResponse("".join(lines))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

async def settings_calibration(request: Request):
    summary = get_calibration_summary()
    pipeline_calibration = compute_calibration()
    return render(
        "calibration.html",
        request=request,
        summary=summary,
        pipeline_calibration=pipeline_calibration,
    )


async def settings_progress(request: Request):
    """Board-facing funnel/quality view (see app/services/progress_service.py)."""
    from app.services.progress_service import board_metrics
    return render("settings_progress.html", request=request, progress=board_metrics())


async def settings_health(request: Request):
    """Internal ops view: usage, system health, and data integrity."""
    from app.services.metrics_service import (
        usage_summary, system_health, engine_quality, integrity_checks,
    )
    return render(
        "settings_health.html",
        request=request,
        usage=usage_summary(),
        system=system_health(),
        engine=engine_quality(),
        integrity=integrity_checks(),
    )


async def job_record_outcome(request: Request):
    job_id = int(request.path_params["job_id"])
    form = await request.form()
    outcome = (form.get("outcome") or "").strip()
    notes = (form.get("notes") or "").strip() or None
    try:
        record_outcome(job_id, outcome, notes)
    except ValueError as e:
        return HTMLResponse(f'<div style="color:var(--tier-pass);font-size:13px;">❌ {_html.escape(str(e))}</div>', status_code=400)
    outcomes = get_job_outcomes(job_id)
    tmpl = jinja.get_template("components/outcome_row.html")
    rows = "".join(tmpl.render(o=o) for o in outcomes)
    return HTMLResponse(f'<div id="outcome-list">{rows}</div>')


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

async def favicon(request: Request):
    return Response(status_code=204)


def create_app(batch_research_fn=None, commit_fn=None) -> Starlette:
    routes = [
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/login",  login_get,  methods=["GET"]),
        Route("/login",  login_post, methods=["POST"]),

        Route("/",                            dashboard,             methods=["GET"]),
        Route("/job/score",                   score_job_post,        methods=["POST"]),
        Route("/job/{job_id:int}",            job_detail,            methods=["GET"]),
        Route("/job/{job_id:int}/resume",          job_resume_preview,   methods=["GET"]),
        Route("/job/{job_id:int}/resume/download", job_resume_download,  methods=["GET"]),
        Route("/job/{job_id:int}/cover-letter",          job_cover_letter_generate, methods=["POST"]),
        Route("/job/{job_id:int}/cover-letter",          job_cover_letter_preview,  methods=["GET"]),
        Route("/job/{job_id:int}/cover-letter/download", job_cover_letter_download, methods=["GET"]),
        Route("/job/{job_id:int}/kit",          job_kit,          methods=["GET"]),
        Route("/job/{job_id:int}/kit/download", job_kit_download, methods=["GET"]),
        Route("/job/{job_id:int}/drawer",     job_drawer,            methods=["GET"]),
        Route("/job/{job_id:int}/research-panel", job_research_panel, methods=["GET"]),
        Route("/job/{job_id:int}/brief-panel",    job_brief_panel,    methods=["GET"]),
        Route("/job/{job_id:int}/generate-brief", job_generate_brief, methods=["POST"]),
        Route("/job/{job_id:int}/research",   job_trigger_research,  methods=["POST"]),
        Route("/job/{job_id:int}/stage",      job_update_stage,      methods=["POST"]),
        Route("/job/{job_id:int}/ethics",     job_toggle_ethics,     methods=["POST"]),
        Route("/job/{job_id:int}/notes",      job_save_notes,        methods=["POST"]),
        Route("/job/{job_id:int}/contact",    job_add_contact,       methods=["POST"]),

        Route("/companies",                         companies_list,           methods=["GET"]),
        Route("/companies",                         companies_add,            methods=["POST"]),
        Route("/companies/{co_id:int}/research",    company_trigger_research, methods=["POST"]),
        Route("/companies/{co_id:int}/research-redirect", company_research_redirect, methods=["GET"]),
        Route("/companies/{co_id:int}/detail",      company_detail,           methods=["GET"]),
        Route("/companies/{co_id:int}/status",      company_update_status,    methods=["POST"]),
        Route("/companies/{co_id:int}/promote",     company_promote,          methods=["POST"]),
        Route("/companies/{co_id:int}/refresh-matches", companies_refresh_matches, methods=["POST"]),
        Route("/companies/research-batch",           companies_research_batch, methods=["POST"]),

        Route("/vetting",                     vetting_view,          methods=["GET"]),
        Route("/rejected",                    rejected_view,         methods=["GET"]),

        Route("/pipeline",                    pipeline_view,         methods=["GET"]),

        Route("/prep",                                             prep_index,                methods=["GET"]),
        Route("/prep/palette",                                     prep_palette,              methods=["GET"]),
        Route("/prep/sessions/{session_id:int}/select",           session_select,            methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/hook",             session_set_hook,          methods=["PATCH"]),
        Route("/prep/sessions/{session_id:int}/schedule",         session_set_schedule,      methods=["PATCH"]),
        Route("/prep/sessions/{session_id:int}/interviewers",     session_set_interviewers,  methods=["PATCH"]),
        Route("/prep/sessions/{session_id:int}/scratchpad",       session_set_scratchpad,    methods=["PATCH"]),
        Route("/prep/sessions/{session_id:int}/transcript",       session_set_transcript,    methods=["PATCH"]),
        Route("/prep/sessions/{session_id:int}/questions-to-ask", question_to_ask_create,   methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/questions-they-ask", question_they_ask_create, methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/questions-they-ask/generate", question_they_ask_generate, methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/red-flags",        red_flag_create,           methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/anchors/{anchor_id}", anchor_pin,             methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/anchors/{anchor_id}", anchor_unpin,           methods=["DELETE"]),
        Route("/prep/sessions/{session_id:int}/draft",            session_draft,             methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/analyze",          transcript_analyze,        methods=["POST"]),
        Route("/prep/sessions/{session_id:int}/cheat-sheet",      cheat_sheet_print,         methods=["GET"]),
        Route("/prep/sessions/{session_id:int}",                  session_update,            methods=["PATCH"]),
        Route("/prep/sessions/{session_id:int}",                  session_delete,            methods=["DELETE"]),
        Route("/prep/questions-to-ask/{q_id:int}",                question_to_ask_update,    methods=["PATCH"]),
        Route("/prep/questions-to-ask/{q_id:int}",                question_to_ask_delete,    methods=["DELETE"]),
        Route("/prep/questions-they-ask/{q_id:int}",              question_they_ask_update,  methods=["PATCH"]),
        Route("/prep/questions-they-ask/{q_id:int}",              question_they_ask_delete,  methods=["DELETE"]),
        Route("/prep/red-flags/{flag_id:int}",                    red_flag_delete,           methods=["DELETE"]),
        Route("/prep/{job_id:int}/sessions",                      session_create,            methods=["POST"]),
        Route("/prep/{job_id:int}/sessions/reorder",              sessions_reorder,          methods=["POST"]),
        Route("/prep/{job_id:int}",                               prep_for_job,              methods=["GET"]),

        Route("/followups/widget",                    followups_widget,      methods=["GET"]),
        Route("/followups/snooze-all",                followups_snooze_all,  methods=["POST"]),
        Route("/followups/{job_id:int}/log",          followup_log,          methods=["POST"]),
        Route("/followups/{job_id:int}/heard-back",   followup_heard_back,   methods=["POST"]),
        Route("/followups/{job_id:int}/snooze",       followup_snooze,       methods=["POST"]),
        Route("/followups/{job_id:int}/close",        followup_close,        methods=["POST"]),

        Route("/recruiters",                  recruiters_list,       methods=["GET"]),
        Route("/recruiters",                  recruiters_add,        methods=["POST"]),
        Route("/recruiters/{r_id:int}/detail",  recruiter_detail,      methods=["GET"]),
        Route("/recruiters/{r_id:int}/contact", recruiters_log_contact, methods=["POST"]),

        Route("/settings",                            settings_view,             methods=["GET"]),
        Route("/settings/rubric",                     settings_rubric,           methods=["GET"]),
        Route("/settings/methodology",                settings_methodology,      methods=["GET"]),
        Route("/guide",                               guide_view,                methods=["GET"]),
        Route("/settings/test-slack",                 settings_test_slack,       methods=["POST"]),
        Route("/settings/send-digest",                settings_send_digest,      methods=["POST"]),
        Route("/settings/fix-sheet-headers",          fix_sheet_headers,         methods=["POST"]),
        Route("/settings/filters",                    settings_filters_list,     methods=["GET"]),
        Route("/settings/filters/add",                settings_filters_add,      methods=["POST"]),
        Route("/settings/filters/{f_id:int}/toggle",  settings_filters_toggle,   methods=["POST"]),
        Route("/settings/filters/{f_id:int}/delete",  settings_filters_delete,   methods=["POST"]),
        Route("/settings/no-go",                      settings_no_go_list,       methods=["GET"]),
        Route("/settings/no-go/add",                  settings_no_go_add,        methods=["POST"]),
        Route("/settings/no-go/{n_id:int}/toggle",    settings_no_go_toggle,     methods=["POST"]),
        Route("/settings/no-go/{n_id:int}/delete",    settings_no_go_delete,     methods=["POST"]),
        Route("/settings/ethics",                     settings_ethics_list,      methods=["GET"]),
        Route("/settings/ethics/add",                 settings_ethics_add,       methods=["POST"]),
        Route("/settings/ethics/{e_id:int}/toggle",   settings_ethics_toggle,    methods=["POST"]),
        Route("/settings/ethics/{e_id:int}/delete",   settings_ethics_delete,    methods=["POST"]),
        Route("/settings/profile/save",               settings_profile_save,     methods=["POST"]),
        Route("/prep/backfill-themes",        backfill_question_themes, methods=["POST"]),
        Route("/sync",                        manual_sync,           methods=["POST"]),

        Route("/admin/patterns",              admin_patterns_get,    methods=["GET"]),
        Route("/admin/rescore-all",           admin_rescore_get,     methods=["GET"]),
        Route("/admin/rescore-all",           admin_rescore_post,    methods=["POST"]),
        Route("/admin/import-tier-a",         admin_import_tier_a,   methods=["POST"]),

        Route("/discovered",                  discovered_view,       methods=["GET"]),
        Route("/discovered/add-linkedin",     linkedin_add,          methods=["POST"]),
        Route("/discovered/{job_id:int}/stage",     job_stage_update,           methods=["POST"]),
        Route("/job/{job_id:int}/promote",          job_promote_from_discovery, methods=["POST"]),
        Route("/job/{job_id:int}/dismiss",          job_dismiss_from_discovery, methods=["POST"]),
        Route("/job/{job_id:int}/close-listing",    job_close_listing,          methods=["POST"]),
        Route("/job/{job_id:int}/fetch-and-score",  job_fetch_and_score,        methods=["POST"]),
        Route("/job/{job_id:int}/paste-and-score",  job_paste_and_score,        methods=["POST"]),
        Route("/job/{job_id:int}/rescore",          job_rescore,                methods=["POST"]),
        Route("/admin/retry-stubs",                 admin_retry_stubs,          methods=["POST"]),
        Route("/discovered/{job_id:int}/detail", discovered_detail,    methods=["GET"]),
        Route("/discovered/{job_id:int}/row",    discovered_row,        methods=["GET"]),
        Route("/targets",                     targets_list,          methods=["GET"]),
        Route("/targets",                     targets_add,           methods=["POST"]),
        Route("/targets/{co_id:int}/detail",  target_detail,         methods=["GET"]),
        Route("/targets/{co_id:int}/toggle",  targets_toggle,        methods=["POST"]),
        Route("/targets/{co_id:int}/scan-now", targets_scan_now,     methods=["POST"]),
        Route("/targets/{co_id:int}/refresh-matches", targets_refresh_matches, methods=["POST"]),
        Route("/targets/{co_id:int}/research", targets_research,     methods=["POST"]),
        Route("/api/discovery/scan",          api_discovery_scan,    methods=["POST"]),
        Route("/api/task-status",             api_task_status,        methods=["GET"]),
        Route("/api/sync-status",             api_sync_status,        methods=["GET"]),
        Route("/settings/calibration",        settings_calibration,   methods=["GET"]),
        Route("/settings/progress",           settings_progress,      methods=["GET"]),
        Route("/settings/health",             settings_health,        methods=["GET"]),
        Route("/job/{job_id:int}/outcome",    job_record_outcome,     methods=["POST"]),
    ]

    app = Starlette(
        routes=routes,
        # UsageTrackingMiddleware is outermost so its timing wraps the full stack
        # (auth/CSRF included) and every request — even a 302/403 — is recorded.
        middleware=[
            Middleware(UsageTrackingMiddleware),
            Middleware(SecurityHeadersMiddleware),
            Middleware(CSRFValidationMiddleware),
            Middleware(AuthMiddleware),
            # Innermost: wraps the route handler directly so it sees the real
            # response status before any outer middleware touches it.
            Middleware(VolumeCommitMiddleware, commit_fn=commit_fn),
        ],
        exception_handlers={Exception: unhandled_exception_handler},
    )
    app.state.batch_research_fn = batch_research_fn
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    return app
