"""Interview prep helpers: scheduling, context building, upcoming interviews."""

from datetime import datetime, timedelta, timezone

PREP_ELIGIBLE_STAGES = {'discovered', 'evaluated', 'researching', 'outreach', 'on_hold', 'recruiter', 'hm_interview', 'panel', 'final_offer'}


def format_schedule(s: dict) -> str:
    """Build a display string for session schedule. e.g. '2026-05-28 · 14:00 PT · Video'."""
    if not s:
        return ''
    parts = []
    if s.get('schedule_date'):
        parts.append(s['schedule_date'])
    if s.get('schedule_time'):
        time_str = s['schedule_time']
        if s.get('schedule_tz'):
            time_str = time_str + ' ' + s['schedule_tz']
        parts.append(time_str)
    if s.get('schedule_mode'):
        parts.append(s['schedule_mode'])
    return ' · '.join(parts)


def _humanize_relative(td):
    """Convert timedelta to human-readable relative string."""
    hours = td.total_seconds() / 3600
    if hours < 0:
        return 'now'
    if hours < 1:
        return 'in <1h'
    if hours < 24:
        return f'in {int(hours)}h'
    return f'in {int(hours/24)}d'


def get_upcoming_interviews(conn, limit=4):
    """
    Return upcoming sessions sorted by datetime ascending.
    Excludes sessions more than 30 min in the past.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=30)).isoformat()

    rows = conn.execute("""
        SELECT s.*, j.company, j.job_title, j.pipeline_stage
        FROM interview_sessions s
        JOIN jobs j ON j.id = s.job_id
        WHERE s.schedule_date IS NOT NULL
          AND s.schedule_date != ''
          AND j.pipeline_stage IN ('evaluated','researching','outreach','recruiter','hm_interview','panel','final_offer')
          AND j.auto_rejected = 0
        ORDER BY s.schedule_date, s.schedule_time
        LIMIT ?
    """, (limit * 2,)).fetchall()

    out = []
    for r in rows:
        date_str = r['schedule_date']
        time_str = r['schedule_time'] or '09:00'
        try:
            when = datetime.fromisoformat(f"{date_str}T{time_str}:00")
        except ValueError:
            continue
        if when.isoformat() < cutoff:
            continue
        out.append({
            **dict(r),
            'when': when,
            'when_display': format_schedule(dict(r)),
            'rel': _humanize_relative(when - datetime.now()),
        })

    return out[:limit]


def build_session_context(conn, session_id: int) -> dict:
    """Load full session context: session, job, and all child rows."""
    session = conn.execute("SELECT * FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        return None

    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (session['job_id'],)).fetchone()
    questions_to_ask = conn.execute(
        "SELECT * FROM session_questions_to_ask WHERE session_id = ? ORDER BY position", (session_id,)
    ).fetchall()
    questions_they_ask = conn.execute(
        "SELECT * FROM session_questions_they_ask WHERE session_id = ? ORDER BY position", (session_id,)
    ).fetchall()
    red_flags = conn.execute(
        "SELECT * FROM session_red_flags WHERE session_id = ? ORDER BY position", (session_id,)
    ).fetchall()
    pinned_ids = {r['anchor_id'] for r in conn.execute(
        "SELECT anchor_id FROM session_pinned_anchors WHERE session_id = ?", (session_id,)
    ).fetchall()}
    all_anchors = conn.execute(
        "SELECT * FROM anchor_stories ORDER BY strongest DESC, id"
    ).fetchall()

    return {
        'session': dict(session),
        'job': dict(job),
        'questions_to_ask': [dict(q) for q in questions_to_ask],
        'questions_they_ask': [dict(q) for q in questions_they_ask],
        'red_flags': [dict(r) for r in red_flags],
        'all_anchors': [dict(a) for a in all_anchors],
        'pinned_anchor_ids': pinned_ids,
    }
