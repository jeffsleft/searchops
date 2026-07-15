"""Interview Prep routes — per-company, per-session preparation system."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.models import get_db
from app.providers import get_provider
from app.pipeline.session_seeds import SESSION_TYPES, STAGE_TO_DEFAULT_TYPE, seed_session_content, seed_pinned_anchors
from app.pipeline.prep import PREP_ELIGIBLE_STAGES, build_session_context

TEMPLATES_DIR = Path(__file__).parent / "templates"
jinja_prep = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)
jinja_prep.filters['fromjson'] = json.loads
jinja_prep.globals['now'] = lambda: datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def render_prep(template: str, **ctx) -> HTMLResponse:
    """Render a prep template."""
    return HTMLResponse(jinja_prep.get_template(template).render(**ctx))


def _render_prep_main(job_id: int, active_session_id: int, request=None) -> HTMLResponse:
    """Load full context and render the main prep panel (session tabs + body)."""
    with get_db() as conn:
        job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        sessions = [dict(r) for r in conn.execute(
            "SELECT * FROM interview_sessions WHERE job_id = ? ORDER BY position", (job_id,)
        ).fetchall()]

        active = next((s for s in sessions if s['id'] == active_session_id), sessions[0] if sessions else None)
        ctx = {}
        if active:
            ctx = build_session_context(conn, active['id'])

        default_type = STAGE_TO_DEFAULT_TYPE.get(job.get('pipeline_stage'))
        has_session_of_default_type = any(s['type_id'] == default_type for s in sessions)
        stage_prompt_type = (SESSION_TYPES.get(default_type) if default_type and not has_session_of_default_type else None)
        stage_prompt_type_id = default_type if stage_prompt_type else None

        all_anchors = [dict(a) for a in conn.execute("SELECT * FROM anchor_stories ORDER BY strongest DESC, id").fetchall()]

        eligible_jobs = [dict(r) for r in conn.execute("""
            SELECT id, company, job_title, final_score, pipeline_stage
            FROM jobs
            WHERE pipeline_stage IN ('discovered','evaluated','researching','outreach','applied','on_hold','recruiter','hm_interview','panel','final_offer')
              AND auto_rejected = 0
            ORDER BY final_score DESC
        """).fetchall()]

    active_sid = active['id'] if active else None
    # HTMX rail/session controls target ".prep-shell" (outerHTML swap), so an HTMX
    # request must return ONLY that fragment — returning the full base.html page
    # nests a second app shell inside the content area. A direct (non-HTMX) load of
    # /prep/{id} (e.g. the HX-Push-Url) still needs the full page.
    template = "prep/_shell.html" if (request and request.headers.get("HX-Request")) else "prep/index.html"
    return render_prep(template,
        request=request,
        active_job=job,
        eligible_jobs=eligible_jobs,
        sessions=sessions,
        active_session_id=active_sid,
        active_session=ctx.get('session') if active else None,
        questions_to_ask=ctx.get('questions_to_ask', []),
        questions_they_ask=ctx.get('questions_they_ask', []),
        red_flags=ctx.get('red_flags', []),
        all_anchors=all_anchors,
        pinned_anchor_ids=ctx.get('pinned_anchor_ids', set()),
        stage_prompt_type=stage_prompt_type,
        stage_prompt_type_id=stage_prompt_type_id,
        session_types=SESSION_TYPES,
        layout='rail',
    )


# ===== Main pages =====

async def prep_index(request: Request):
    """GET /prep — Show empty hero or list of eligible companies."""
    with get_db() as conn:
        eligible = [dict(r) for r in conn.execute("""
            SELECT id, company, job_title, final_score, pipeline_stage, auto_rejected
            FROM jobs
            WHERE pipeline_stage IN ('discovered','evaluated','researching','outreach','applied','on_hold','recruiter','hm_interview','panel','final_offer')
              AND auto_rejected = 0
            ORDER BY final_score DESC
        """).fetchall()]

    return render_prep("prep/index.html",
        request=request,
        active_job=None,
        eligible_jobs=eligible,
        sessions=[],
        layout='rail',
    )


async def prep_for_job(request: Request):
    """GET /prep/{job_id} — Load a specific job and auto-select or create first session."""
    job_id = int(request.path_params['job_id'])
    session_id = request.query_params.get('session')

    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return HTMLResponse("Job not found", status_code=404)

        job = dict(job)
        if job['pipeline_stage'] not in PREP_ELIGIBLE_STAGES or job['auto_rejected']:
            return HTMLResponse("This job is not eligible for prep", status_code=400)

        # Get existing sessions
        sessions = [dict(r) for r in conn.execute(
            "SELECT * FROM interview_sessions WHERE job_id = ? ORDER BY position", (job_id,)
        ).fetchall()]

        # Auto-create a default session if none exist
        if not sessions:
            default_type = STAGE_TO_DEFAULT_TYPE.get(job['pipeline_stage'], 'recruiter')
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute("""
                INSERT INTO interview_sessions (job_id, type_id, label, position, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (job_id, default_type, SESSION_TYPES[default_type]['label'], 0, now, now))
            session_id = cursor.lastrowid

            # Seed content and anchors
            seed_session_content(conn, session_id, default_type)
            seed_pinned_anchors(conn, session_id)

            sessions = [dict(r) for r in conn.execute(
                "SELECT * FROM interview_sessions WHERE job_id = ? ORDER BY position", (job_id,)
            ).fetchall()]

    # Determine active session
    if not session_id:
        session_id = sessions[0]['id'] if sessions else None
    elif not any(s['id'] == int(session_id) for s in sessions):
        session_id = sessions[0]['id'] if sessions else None
    else:
        session_id = int(session_id)

    # Build response with HX-Push-Url
    response = _render_prep_main(job_id, session_id, request=request)
    if session_id:
        response.headers['HX-Push-Url'] = f'/prep/{job_id}?session={session_id}'
    return response


# ===== Session CRUD =====

async def session_create(request: Request):
    """POST /prep/{job_id}/sessions — Create a new session."""
    job_id = int(request.path_params['job_id'])
    form = await request.form()
    type_id = form.get('type_id', 'custom')

    if type_id not in SESSION_TYPES:
        return HTMLResponse("Invalid session type", status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    label = SESSION_TYPES[type_id]['label']

    with get_db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM interview_sessions WHERE job_id = ?",
            (job_id,)
        ).fetchone()[0]

        cursor = conn.execute("""
            INSERT INTO interview_sessions (job_id, type_id, label, position, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, type_id, label, max_pos, now, now))
        session_id = cursor.lastrowid

        seed_session_content(conn, session_id, type_id)
        seed_pinned_anchors(conn, session_id)

    response = _render_prep_main(job_id, session_id, request=request)
    response.headers['HX-Push-Url'] = f'/prep/{job_id}?session={session_id}'
    return response


async def session_select(request: Request):
    """POST /prep/sessions/{session_id}/select — Switch to a different session."""
    session_id = int(request.path_params['session_id'])

    with get_db() as conn:
        session = conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            return HTMLResponse("Session not found", status_code=404)
        job_id = session['job_id']

    response = _render_prep_main(job_id, session_id, request=request)
    response.headers['HX-Push-Url'] = f'/prep/{job_id}?session={session_id}'
    return response


async def session_update(request: Request):
    """PATCH /prep/sessions/{session_id} — Update session label."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    label = form.get('label', '').strip()

    if not label:
        return HTMLResponse("Label cannot be empty", status_code=400)

    with get_db() as conn:
        conn.execute(
            "UPDATE interview_sessions SET label = ?, updated_at = ? WHERE id = ?",
            (label, datetime.now(timezone.utc).isoformat(), session_id),
        )

    return HTMLResponse("")


async def session_delete(request: Request):
    """DELETE /prep/sessions/{session_id} — Delete a session (auto-create recruiter if last)."""
    session_id = int(request.path_params['session_id'])

    with get_db() as conn:
        session = conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            return HTMLResponse("Session not found", status_code=404)

        job_id = session['job_id']

        # Delete the session
        conn.execute("DELETE FROM interview_sessions WHERE id = ?", (session_id,))

        # Check if any sessions remain
        remaining = conn.execute(
            "SELECT COUNT(*) FROM interview_sessions WHERE job_id = ?", (job_id,)
        ).fetchone()[0]

        # Auto-create recruiter if this was the last session
        active_session_id = None
        if remaining == 0:
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute("""
                INSERT INTO interview_sessions (job_id, type_id, label, position, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (job_id, 'recruiter', SESSION_TYPES['recruiter']['label'], 0, now, now))
            active_session_id = cursor.lastrowid
            seed_session_content(conn, active_session_id, 'recruiter')
            seed_pinned_anchors(conn, active_session_id)
        else:
            first = conn.execute(
                "SELECT id FROM interview_sessions WHERE job_id = ? ORDER BY position LIMIT 1", (job_id,)
            ).fetchone()
            active_session_id = first['id'] if first else None

    return _render_prep_main(job_id, active_session_id, request=request)


async def sessions_reorder(request: Request):
    """POST /prep/{job_id}/sessions/reorder — Reorder sessions by position."""
    job_id = int(request.path_params['job_id'])
    form = await request.form()
    order_str = form.get('order', '')

    if not order_str:
        return HTMLResponse("", status_code=400)

    try:
        order = [int(x.strip()) for x in order_str.split(',')]
    except ValueError:
        return HTMLResponse("Invalid order", status_code=400)

    with get_db() as conn:
        for pos, session_id in enumerate(order):
            conn.execute(
                "UPDATE interview_sessions SET position = ? WHERE id = ? AND job_id = ?",
                (pos, session_id, job_id),
            )

    return _render_prep_main(job_id, order[0] if order else 0, request=request)


# ===== Session content fields =====

async def session_set_hook(request: Request):
    """PATCH /prep/sessions/{session_id}/hook — Update opening_hook."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    value = form.get('value', '')

    with get_db() as conn:
        conn.execute(
            "UPDATE interview_sessions SET opening_hook = ?, updated_at = ? WHERE id = ?",
            (value, datetime.now(timezone.utc).isoformat(), session_id),
        )

    return HTMLResponse("")


async def session_set_schedule(request: Request):
    """PATCH /prep/sessions/{session_id}/schedule — Update schedule fields (partial)."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()

    updates = {}
    if 'date' in form:
        updates['schedule_date'] = form.get('date', '')
    if 'time' in form:
        updates['schedule_time'] = form.get('time', '')
    if 'tz' in form:
        updates['schedule_tz'] = form.get('tz', '')
    if 'mode' in form:
        updates['schedule_mode'] = form.get('mode', '')
    if 'link' in form:
        updates['schedule_link'] = form.get('link', '')

    if updates:
        updates['updated_at'] = datetime.now(timezone.utc).isoformat()
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [session_id]
        with get_db() as conn:
            conn.execute(f"UPDATE interview_sessions SET {set_clause} WHERE id = ?", values)

    return HTMLResponse("")


async def session_set_interviewers(request: Request):
    """PATCH /prep/sessions/{session_id}/interviewers — Update interviewers_notes."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    value = form.get('value', '')

    with get_db() as conn:
        conn.execute(
            "UPDATE interview_sessions SET interviewers_notes = ?, updated_at = ? WHERE id = ?",
            (value, datetime.now(timezone.utc).isoformat(), session_id),
        )

    return HTMLResponse("")


async def session_set_scratchpad(request: Request):
    """PATCH /prep/sessions/{session_id}/scratchpad — Update scratchpad."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    value = form.get('value', '')

    with get_db() as conn:
        conn.execute(
            "UPDATE interview_sessions SET scratchpad = ?, updated_at = ? WHERE id = ?",
            (value, datetime.now(timezone.utc).isoformat(), session_id),
        )

    return HTMLResponse("")


async def session_set_transcript(request: Request):
    """PATCH /prep/sessions/{session_id}/transcript — Update transcript."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    value = form.get('value', '')

    with get_db() as conn:
        conn.execute(
            "UPDATE interview_sessions SET transcript = ?, updated_at = ? WHERE id = ?",
            (value, datetime.now(timezone.utc).isoformat(), session_id),
        )

    return HTMLResponse("")


# ===== Questions to ask =====

async def question_to_ask_create(request: Request):
    """POST /prep/sessions/{session_id}/questions-to-ask — Create a question to ask."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    text = form.get('text', '').strip()

    if not text:
        return HTMLResponse("Text cannot be empty", status_code=400)

    with get_db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM session_questions_to_ask WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]

        conn.execute("""
            INSERT INTO session_questions_to_ask (session_id, text, priority, persona, position)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, text, 'Medium', 'Any', max_pos))

        questions = [dict(r) for r in conn.execute(
            "SELECT * FROM session_questions_to_ask WHERE session_id = ? ORDER BY position", (session_id,)
        ).fetchall()]
        active_session = dict(conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone())

    return render_prep("prep/_card_questions_to_ask.html", questions_to_ask=questions, active_session=active_session)


async def question_to_ask_update(request: Request):
    """PATCH /prep/questions-to-ask/{q_id} — Update a question (asked, answer, text)."""
    q_id = int(request.path_params['q_id'])
    form = await request.form()

    updates = {}
    if 'asked' in form:
        updates['asked'] = 1 if form.get('asked') == 'on' else 0
    if 'answer' in form:
        updates['answer'] = form.get('answer', '')
    if 'text' in form:
        updates['text'] = form.get('text', '')

    if updates:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [q_id]
        with get_db() as conn:
            conn.execute(f"UPDATE session_questions_to_ask SET {set_clause} WHERE id = ?", values)

    return HTMLResponse("")


async def question_to_ask_delete(request: Request):
    """DELETE /prep/questions-to-ask/{q_id} — Delete a question to ask."""
    q_id = int(request.path_params['q_id'])

    with get_db() as conn:
        q = conn.execute("SELECT session_id FROM session_questions_to_ask WHERE id = ?", (q_id,)).fetchone()
        if not q:
            return HTMLResponse("Not found", status_code=404)

        session_id = q['session_id']
        conn.execute("DELETE FROM session_questions_to_ask WHERE id = ?", (q_id,))

        questions = [dict(r) for r in conn.execute(
            "SELECT * FROM session_questions_to_ask WHERE session_id = ? ORDER BY position", (session_id,)
        ).fetchall()]
        active_session = dict(conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone())

    return render_prep("prep/_card_questions_to_ask.html", questions_to_ask=questions, active_session=active_session)


# ===== Questions they'll ask =====

async def question_they_ask_create(request: Request):
    """POST /prep/sessions/{session_id}/questions-they-ask — Create a likely question."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    prompt = form.get('prompt', '').strip()

    if not prompt:
        return HTMLResponse("Prompt cannot be empty", status_code=400)

    with get_db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM session_questions_they_ask WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]

        conn.execute("""
            INSERT INTO session_questions_they_ask (session_id, prompt, position)
            VALUES (?, ?, ?)
        """, (session_id, prompt, max_pos))

        questions = [dict(r) for r in conn.execute(
            "SELECT * FROM session_questions_they_ask WHERE session_id = ? ORDER BY position", (session_id,)
        ).fetchall()]

    return render_prep("prep/_card_questions_they_ask.html", questions_they_ask=questions, active_session={'id': session_id})


async def question_they_ask_update(request: Request):
    """PATCH /prep/questions-they-ask/{q_id} — Update a likely question (prompt, answer)."""
    q_id = int(request.path_params['q_id'])
    form = await request.form()

    updates = {}
    if 'prompt' in form:
        updates['prompt'] = form.get('prompt', '')
    if 'my_answer' in form:
        updates['my_answer'] = form.get('my_answer', '')

    if updates:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [q_id]
        with get_db() as conn:
            conn.execute(f"UPDATE session_questions_they_ask SET {set_clause} WHERE id = ?", values)

    return HTMLResponse("")


async def question_they_ask_delete(request: Request):
    """DELETE /prep/questions-they-ask/{q_id} — Delete a likely question."""
    q_id = int(request.path_params['q_id'])

    with get_db() as conn:
        q = conn.execute("SELECT session_id FROM session_questions_they_ask WHERE id = ?", (q_id,)).fetchone()
        if not q:
            return HTMLResponse("Not found", status_code=404)

        session_id = q['session_id']
        conn.execute("DELETE FROM session_questions_they_ask WHERE id = ?", (q_id,))

        questions = [dict(r) for r in conn.execute(
            "SELECT * FROM session_questions_they_ask WHERE session_id = ? ORDER BY position", (session_id,)
        ).fetchall()]

    return render_prep("prep/_card_questions_they_ask.html", questions_they_ask=questions, active_session={'id': session_id})


async def question_they_ask_generate(request: Request):
    """POST /prep/sessions/{session_id}/questions-they-ask/generate — LLM auto-generate likely questions."""
    session_id = int(request.path_params['session_id'])

    with get_db() as conn:
        session = conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (session['job_id'],)).fetchone()

        prompt = f"""You are helping prepare for a job interview. Generate 6 likely questions the interviewer will ask, based on this role:

Company: {job['company']}
Role: {job['job_title']}
Sector: {job.get('sector', 'Unknown')}
Pipeline stage: {job['pipeline_stage']}
Angle: {job.get('recommended_angle', 'general fit')}

Return ONLY a JSON array of strings (questions only). Example: ["Question 1?", "Question 2?"]"""

        try:
            text = get_provider().generate(prompt)
            match = re.search(r'\[[\s\S]*\]', text)
            arr = json.loads(match.group(0)) if match else []

            max_pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM session_questions_they_ask WHERE session_id = ?",
                (session_id,)
            ).fetchone()[0]

            for i, q in enumerate(arr[:8]):
                conn.execute("""
                    INSERT INTO session_questions_they_ask (session_id, prompt, position)
                    VALUES (?, ?, ?)
                """, (session_id, q, max_pos + i))

            questions = [dict(r) for r in conn.execute(
                "SELECT * FROM session_questions_they_ask WHERE session_id = ? ORDER BY position", (session_id,)
            ).fetchall()]

            return render_prep("prep/_card_questions_they_ask.html", questions_they_ask=questions, active_session={'id': session_id})
        except Exception as e:
            return HTMLResponse(f"Error generating questions: {str(e)}", status_code=500)


# ===== Red flags =====

async def red_flag_create(request: Request):
    """POST /prep/sessions/{session_id}/red-flags — Add a red flag."""
    session_id = int(request.path_params['session_id'])
    form = await request.form()
    text = form.get('text', '').strip()

    if not text:
        return HTMLResponse("Text cannot be empty", status_code=400)

    with get_db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM session_red_flags WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]

        conn.execute("""
            INSERT INTO session_red_flags (session_id, text, position)
            VALUES (?, ?, ?)
        """, (session_id, text, max_pos))

        flags = [dict(r) for r in conn.execute(
            "SELECT * FROM session_red_flags WHERE session_id = ? ORDER BY position", (session_id,)
        ).fetchall()]

    return render_prep("prep/_card_red_flags.html", red_flags=flags, session_id=session_id)


async def red_flag_delete(request: Request):
    """DELETE /prep/red-flags/{flag_id} — Delete a red flag."""
    flag_id = int(request.path_params['flag_id'])

    with get_db() as conn:
        flag = conn.execute("SELECT session_id FROM session_red_flags WHERE id = ?", (flag_id,)).fetchone()
        if not flag:
            return HTMLResponse("Not found", status_code=404)

        session_id = flag['session_id']
        conn.execute("DELETE FROM session_red_flags WHERE id = ?", (flag_id,))

        flags = [dict(r) for r in conn.execute(
            "SELECT * FROM session_red_flags WHERE session_id = ? ORDER BY position", (session_id,)
        ).fetchall()]

    return render_prep("prep/_card_red_flags.html", red_flags=flags, session_id=session_id)


# ===== Anchor stories =====

async def anchor_pin(request: Request):
    """POST /prep/sessions/{session_id}/anchors/{anchor_id} — Pin an anchor."""
    session_id = int(request.path_params['session_id'])
    anchor_id = request.path_params['anchor_id']

    with get_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO session_pinned_anchors (session_id, anchor_id)
            VALUES (?, ?)
        """, (session_id, anchor_id))

        all_anchors = [dict(r) for r in conn.execute(
            "SELECT * FROM anchor_stories ORDER BY strongest DESC, id"
        ).fetchall()]
        pinned_ids = {r['anchor_id'] for r in conn.execute(
            "SELECT anchor_id FROM session_pinned_anchors WHERE session_id = ?", (session_id,)
        ).fetchall()}

    return render_prep("prep/_card_anchor_stories.html", all_anchors=all_anchors, pinned_anchor_ids=pinned_ids, session_id=session_id)


async def anchor_unpin(request: Request):
    """DELETE /prep/sessions/{session_id}/anchors/{anchor_id} — Unpin an anchor."""
    session_id = int(request.path_params['session_id'])
    anchor_id = request.path_params['anchor_id']

    with get_db() as conn:
        conn.execute(
            "DELETE FROM session_pinned_anchors WHERE session_id = ? AND anchor_id = ?",
            (session_id, anchor_id),
        )

        all_anchors = [dict(r) for r in conn.execute(
            "SELECT * FROM anchor_stories ORDER BY strongest DESC, id"
        ).fetchall()]
        pinned_ids = {r['anchor_id'] for r in conn.execute(
            "SELECT anchor_id FROM session_pinned_anchors WHERE session_id = ?", (session_id,)
        ).fetchall()}

    return render_prep("prep/_card_anchor_stories.html", all_anchors=all_anchors, pinned_anchor_ids=pinned_ids, session_id=session_id)


# ===== Draft & analyze =====

async def session_draft(request: Request):
    """POST /prep/sessions/{session_id}/draft — Auto-draft hook and top-up questions."""
    session_id = int(request.path_params['session_id'])

    with get_db() as conn:
        session = conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (session['job_id'],)).fetchone()

        # Generate opening hook if empty
        if not session['opening_hook']:
            prompt = f"""For an interview with {job['company']} ({job['job_title']}, {job.get('sector', 'Unknown')}),
draft a one-sentence opening hook for a {SESSION_TYPES[session['type_id']]['label']} session.
Angle from the candidate's pipeline notes: {job.get('recommended_angle', 'N/A')}
Return ONLY the hook in quotes, no preamble."""
            try:
                hook = get_provider().generate(prompt).strip()
                conn.execute(
                    "UPDATE interview_sessions SET opening_hook = ?, updated_at = ? WHERE id = ?",
                    (hook, datetime.now(timezone.utc).isoformat(), session_id),
                )
            except Exception:
                pass

        # Top-up questions if fewer than 5
        qcount = conn.execute(
            "SELECT COUNT(*) FROM session_questions_to_ask WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        if qcount < 5:
            seed_session_content(conn, session_id, session['type_id'])

    return _render_prep_main(job['id'], session_id, request=request)


async def transcript_analyze(request: Request):
    """POST /prep/sessions/{session_id}/analyze — Analyze transcript with LLM."""
    session_id = int(request.path_params['session_id'])

    with get_db() as conn:
        session = conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()

        if not session['transcript']:
            return HTMLResponse("No transcript", status_code=400)

        prompt = f"""Analyze this interview transcript. Return ONLY a JSON object with this exact shape:
{{"answered":[{{"q":"question I asked","a":"their answer summary"}}],"newQuestions":["new question I should ask next round"],"signals":[{{"type":"positive|negative|neutral","note":"signal observation"}}]}}

Transcript:
{session['transcript'][:12000]}"""

        try:
            text = get_provider().generate(prompt)
            match = re.search(r'\{[\s\S]*\}', text)
            insights = json.loads(match.group(0)) if match else None

            conn.execute(
                "UPDATE interview_sessions SET transcript_insights_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(insights) if insights else None, datetime.now(timezone.utc).isoformat(), session_id),
            )

            ctx = build_session_context(conn, session_id)
        except Exception as e:
            return HTMLResponse(f"Error analyzing: {str(e)}", status_code=500)

    return render_prep("prep/_card_transcript.html",
        session=ctx['session'],
        transcript_insights_json=ctx['session'].get('transcript_insights_json'),
    )


# ===== Print & palette =====

async def cheat_sheet_print(request: Request):
    """GET /prep/sessions/{session_id}/cheat-sheet — Print-friendly prep sheet."""
    session_id = int(request.path_params['session_id'])

    with get_db() as conn:
        ctx = build_session_context(conn, session_id)

    if not ctx:
        return HTMLResponse("Session not found", status_code=404)

    return render_prep("prep/cheat_sheet.html", **ctx)


async def prep_palette(request: Request):
    """GET /prep/palette — ⌘K command palette modal."""
    with get_db() as conn:
        eligible = [dict(r) for r in conn.execute("""
            SELECT id, company, job_title, final_score, pipeline_stage, auto_rejected
            FROM jobs
            WHERE pipeline_stage IN ('discovered','evaluated','researching','outreach','applied','on_hold','recruiter','hm_interview','panel','final_offer')
              AND auto_rejected = 0
            ORDER BY final_score DESC
        """).fetchall()]

    return render_prep("prep/_palette.html", eligible_jobs=eligible)
