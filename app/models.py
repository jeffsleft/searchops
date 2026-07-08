import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
from app.config import DATABASE_PATH
from app.security.url_guard import validate_url


def normalize_url(url: str) -> str | None:
    """Remove UTM params, query string, and fragment to create a stable URL for deduplication."""
    if not url:
        return None
    
    try:
        url = validate_url(url)
    except ValueError:
        return None

    UTM_PARAMS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'ref', 'src'}
    p = urlparse(url.strip().rstrip('/'))
    qs = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in UTM_PARAMS]
    return urlunparse(p._replace(query=urlencode(qs), fragment=''))

SCHEMA = """
-- Companies: target companies, may or may not have an open role
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    website TEXT,
    sector TEXT,
    headcount_estimate TEXT,
    funding_stage TEXT,
    why_interesting TEXT,
    fit_score REAL,
    need_assessment TEXT DEFAULT 'Unknown', -- High/Medium/Low/Unknown
    source TEXT DEFAULT 'manual',           -- manual/research
    date_added DATE NOT NULL,
    status TEXT DEFAULT 'Watchlist',        -- Watchlist/Researching/Outreach/Active/Closed
    research_json TEXT,                     -- cached Gemini research result (JSON)
    research_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Jobs: linked to a company; a company can have zero or many jobs
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES companies(id),
    company TEXT NOT NULL,               -- denormalized for display
    job_title TEXT,
    url TEXT,
    jd_text TEXT,
    date_found DATE NOT NULL,
    status TEXT DEFAULT 'Identified',    -- mirrors pipeline stage
    final_score REAL,
    deterministic_score REAL,
    llm_adjustment REAL,
    auto_rejected INTEGER DEFAULT 0,
    reject_reason TEXT,
    pros TEXT,
    cons TEXT,
    greenfield TEXT,                     -- Yes/No/Partial/N/A
    greenfield_rationale TEXT,
    pricing_model TEXT,
    sector TEXT,
    recommended_angle TEXT,
    tech_stack_json TEXT,                -- JSON object
    flags_json TEXT,                     -- JSON array of flag strings
    salary_range_detected TEXT,
    has_fde_model TEXT DEFAULT 'Unknown',
    ethics_vetted INTEGER DEFAULT 0,
    pipeline_stage TEXT DEFAULT 'Identified',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Pipeline history: every stage change
CREATE TABLE IF NOT EXISTS pipeline_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    changed_by TEXT DEFAULT 'jeff'
);

-- Contacts: people at each company
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    company_id INTEGER REFERENCES companies(id),
    name TEXT NOT NULL,
    title TEXT,
    linkedin_url TEXT,
    email TEXT,
    persona_type TEXT,       -- interviewer archetype (The Technical Skeptic, etc.)
    relationship TEXT,       -- 1st_degree/2nd_degree/cold
    notes TEXT,
    met_on DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Questions: interview question bank, per job
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    question TEXT NOT NULL,
    category TEXT NOT NULL,       -- Financial/Strategic/Technical/Cultural/Pricing/Operational
    persona_target TEXT,          -- CFO/CRO/COO/CCO/VP Eng/Founder/Recruiter/Any
    priority TEXT NOT NULL,       -- High/Medium/Low
    status TEXT DEFAULT 'unasked', -- unasked/asked/answered/deferred
    source TEXT NOT NULL,         -- seed/research/transcript/manual
    answer_notes TEXT,
    asked_to TEXT,
    asked_on DATE,
    divergence_flag INTEGER DEFAULT 0,
    divergence_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Strategy briefs: per-company accumulating markdown document
CREATE TABLE IF NOT EXISTS strategy_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    company TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Transcripts: raw interview transcripts + analysis
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    interview_date DATE,
    contact_name TEXT,
    contact_title TEXT,
    round TEXT,
    raw_transcript TEXT,
    granola_analysis TEXT,       -- optional paste from Granola
    gemini_analysis TEXT,        -- Gemini's independent analysis (JSON)
    comparison_result TEXT,      -- Gemini vs Granola comparison (JSON), if both provided
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Interviews: per-interview record with Jeff's performance scorecard
CREATE TABLE IF NOT EXISTS interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    transcript_id INTEGER REFERENCES transcripts(id),
    interview_date DATE,
    round TEXT,
    contacts_json TEXT,          -- JSON array of contact names/titles
    jeff_notes TEXT,
    what_went_well TEXT,
    what_to_improve TEXT,
    anchor_stories_used TEXT,
    confidence_level INTEGER,    -- 1-5
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Recruiters: separate CRM, not linked to a specific company
CREATE TABLE IF NOT EXISTS recruiters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    firm TEXT,
    linkedin_url TEXT,
    email TEXT,
    phone TEXT,
    specialty TEXT,              -- GTM/RevOps/FinOps/General/etc.
    last_contact_date DATE,
    relationship_status TEXT DEFAULT 'Cold', -- Cold/Warm/Active
    notes TEXT,
    stale_alert_days INTEGER DEFAULT 30,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Research cache: TTL-based cache for Gemini company research
CREATE TABLE IF NOT EXISTS research_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL UNIQUE,
    result_json TEXT NOT NULL,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Score history: track score changes over time
CREATE TABLE IF NOT EXISTS score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    final_score REAL,
    deterministic_score REAL,
    llm_adjustment REAL,
    provider TEXT DEFAULT 'gemini'
);

-- Background task log: visibility into Modal background tasks
CREATE TABLE IF NOT EXISTS task_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,   -- 'started' | 'completed' | 'failed' | 'partial'
    message TEXT,
    entity_name TEXT,       -- company/job name when relevant
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Follow-up log: one row per sent follow-up per job
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sent_at TEXT NOT NULL,
    note TEXT,
    UNIQUE(job_id, sent_at)
);
CREATE INDEX IF NOT EXISTS idx_followups_job ON followups(job_id);

-- Anchor stories: global pool, pinned per session
CREATE TABLE IF NOT EXISTS anchor_stories (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  strongest INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Interview sessions: per-company, per-round prep state
CREATE TABLE IF NOT EXISTS interview_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  type_id TEXT NOT NULL CHECK (type_id IN (
    'recruiter','hm','hm_followup','peer','panel','final','presentation','custom'
  )),
  label TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  schedule_date TEXT,
  schedule_time TEXT,
  schedule_tz TEXT,
  schedule_mode TEXT,
  schedule_link TEXT,
  opening_hook TEXT NOT NULL DEFAULT '',
  interviewers_notes TEXT NOT NULL DEFAULT '',
  scratchpad TEXT NOT NULL DEFAULT '',
  transcript TEXT NOT NULL DEFAULT '',
  transcript_insights_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_job ON interview_sessions(job_id, position);
CREATE INDEX IF NOT EXISTS idx_sessions_schedule ON interview_sessions(schedule_date, schedule_time);

CREATE TABLE IF NOT EXISTS session_questions_to_ask (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'Medium' CHECK (priority IN ('High','Medium','Low')),
  persona TEXT NOT NULL DEFAULT 'Any',
  asked INTEGER NOT NULL DEFAULT 0,
  answer TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_questions_to_ask_session ON session_questions_to_ask(session_id, position);

CREATE TABLE IF NOT EXISTS session_questions_they_ask (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
  prompt TEXT NOT NULL,
  my_answer TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_questions_they_ask_session ON session_questions_they_ask(session_id, position);

CREATE TABLE IF NOT EXISTS session_red_flags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_red_flags_session ON session_red_flags(session_id, position);

CREATE TABLE IF NOT EXISTS session_pinned_anchors (
  session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
  anchor_id TEXT NOT NULL,
  PRIMARY KEY (session_id, anchor_id)
);

-- Title filters: positive/negative job title match rules (DB-editable, seeds from hunt_targets.yaml)
CREATE TABLE IF NOT EXISTS title_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filter_type TEXT NOT NULL CHECK(filter_type IN ('positive','negative')),
    value TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(filter_type, value)
);

-- Candidate settings: single JSON blob storing the full candidate profile (DB-editable, seeds from candidate_profile.yaml)
CREATE TABLE IF NOT EXISTS candidate_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    profile_json TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- No-go industries: Layer 1 auto-reject sector keywords (DB-editable, seeds from
-- engine _HARD_NO_KEYWORDS on first run). One row per (sector label, keyword).
CREATE TABLE IF NOT EXISTS no_go_industries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT NOT NULL,
    keyword TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(sector, keyword)
);

-- Ethics reasons: reference checklist shown on the Vetting queue to guide manual
-- ethics confirmation (DB-editable, seeds from DEFAULT_ETHICS_REASONS on first run).
CREATE TABLE IF NOT EXISTS ethics_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(reason)
);

-- Login throttle: per-IP failed-attempt log on the Volume so the brute-force
-- lockout is shared across Modal containers (replaces the in-memory dict).
-- attempted_at is wall-clock epoch seconds (time.time()), not monotonic.
CREATE TABLE IF NOT EXISTS login_attempts (
    ip TEXT NOT NULL,
    attempted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempted_at);

-- WP-N performance: index the hot read paths. Every list/detail view filters
-- jobs/companies by these columns or joins child tables by job_id; without these
-- each query is a full table SCAN (confirmed via EXPLAIN QUERY PLAN). Created
-- idempotently here so they apply to the prod Volume DB on next deploy. The
-- composite (job_id, <time>) indexes also satisfy the ORDER BY, dropping the
-- temp B-tree sort. See scripts/bench_indexes.py for before/after measurements.
CREATE INDEX IF NOT EXISTS idx_jobs_company       ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_stage         ON jobs(pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_jobs_active_score  ON jobs(auto_rejected, final_score);
CREATE INDEX IF NOT EXISTS idx_jobs_date_found    ON jobs(date_found);
CREATE INDEX IF NOT EXISTS idx_pipeline_history_job ON pipeline_history(job_id, changed_at);
CREATE INDEX IF NOT EXISTS idx_score_history_job  ON score_history(job_id, scored_at);
CREATE INDEX IF NOT EXISTS idx_contacts_job       ON contacts(job_id);
CREATE INDEX IF NOT EXISTS idx_contacts_company   ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_questions_job      ON questions(job_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_job    ON transcripts(job_id);
CREATE INDEX IF NOT EXISTS idx_strategy_briefs_job ON strategy_briefs(job_id);
CREATE INDEX IF NOT EXISTS idx_interviews_job     ON interviews(job_id);
CREATE INDEX IF NOT EXISTS idx_companies_status   ON companies(status);
CREATE INDEX IF NOT EXISTS idx_companies_name     ON companies(name);

-- Calibration: tracks real-world outcomes for scored jobs so we can measure
-- how well Layer 2/3 scores predict interview conversion and final outcomes.
CREATE TABLE IF NOT EXISTS application_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    outcome TEXT NOT NULL,  -- 'applied','phone_screen','interview','offer','rejected_them','rejected_me','ghosted'
    notes TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_application_outcomes_job ON application_outcomes(job_id);

-- Observability: one row per unhandled request exception (app/observability).
-- actor_id is the SaaS-shaped seam: a single value today, per-tenant later.
CREATE TABLE IF NOT EXISTS error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    request_id TEXT,
    method TEXT,
    route_template TEXT,
    exc_type TEXT,
    message TEXT,
    actor_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_events_ts ON error_events(ts);

-- Product-usage capture: one row per HTTP request. No bodies, no query strings,
-- no PII — method + matched route template + status + timing only.
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    actor_id TEXT,
    method TEXT NOT NULL,
    route_template TEXT NOT NULL,
    status INTEGER,
    duration_ms INTEGER,
    is_hx INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_events_route ON usage_events(route_template);

-- Progress instrumentation (v2): baselines for repo metrics
CREATE TABLE IF NOT EXISTS progress_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    value TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL
);

-- Progress instrumentation: milestones for narrative tracking
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    completed_on TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- Progress instrumentation: monthly snapshots of funnel metrics
CREATE TABLE IF NOT EXISTS progress_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL UNIQUE,
    jobs_scored INTEGER,
    companies_covered INTEGER,
    apps_sent INTEGER,
    phone_screens INTEGER,
    interviews INTEGER,
    offers INTEGER,
    calibration_hit_rate_json TEXT,
    median_days_to_first_interview REAL,
    raw_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Add discovery columns to companies (safe to re-run)
        migration_stmts = [
            "ALTER TABLE companies ADD COLUMN hunt_enabled INTEGER DEFAULT 0",
            "ALTER TABLE companies ADD COLUMN careers_url TEXT",
            "ALTER TABLE companies ADD COLUMN ats_type TEXT",
            "ALTER TABLE companies ADD COLUMN ats_handle TEXT",
            "ALTER TABLE companies ADD COLUMN last_scanned TEXT",
            "ALTER TABLE companies ADD COLUMN scan_error TEXT",
            "ALTER TABLE jobs ADD COLUMN discovery_source TEXT DEFAULT 'manual'",
            "ALTER TABLE jobs ADD COLUMN fit_bullets TEXT",
            "ALTER TABLE jobs ADD COLUMN lightweight_score REAL",
            "ALTER TABLE jobs ADD COLUMN date_added TEXT",
            # Role archetype classification (Feature 3)
            "ALTER TABLE jobs ADD COLUMN role_archetype TEXT",
            # Layer 2 — Match to Candidate (corpus-grounded)
            "ALTER TABLE jobs ADD COLUMN match_score REAL",
            "ALTER TABLE jobs ADD COLUMN match_summary TEXT",
            "ALTER TABLE jobs ADD COLUMN match_evidence_json TEXT",
            "ALTER TABLE jobs ADD COLUMN match_mismatches_json TEXT",
            "ALTER TABLE jobs ADD COLUMN match_bullets_json TEXT",
            "ALTER TABLE jobs ADD COLUMN match_hooks_json TEXT",
            "ALTER TABLE jobs ADD COLUMN match_tailored_summary TEXT",
            "ALTER TABLE jobs ADD COLUMN differentiator_themes_json TEXT",
            "ALTER TABLE jobs ADD COLUMN adjustment_weights_score REAL",
            # Score history: parallel columns for trend tracking
            "ALTER TABLE score_history ADD COLUMN match_score REAL",
            "ALTER TABLE score_history ADD COLUMN adjustment_weights_score REAL",
            # Posting age flag (Feature 1)
            "ALTER TABLE jobs ADD COLUMN posting_age_days INTEGER",
            "ALTER TABLE jobs ADD COLUMN posting_date_raw TEXT",
            # URL deduplication (Feature 2)
            "ALTER TABLE jobs ADD COLUMN source_url TEXT",
            "DROP INDEX IF EXISTS idx_jobs_source_url",
            "CREATE INDEX IF NOT EXISTS idx_jobs_source_url ON jobs(source_url) WHERE source_url IS NOT NULL",
        ]
        def _run_migration(stmt: str) -> None:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" not in msg and "already exists" not in msg:
                    raise

        for stmt in migration_stmts:
            _run_migration(stmt)
        _run_migration("ALTER TABLE questions ADD COLUMN suggested_themes TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN interview_probability TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN interview_probability_rationale TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN timing_signal TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN timing_signal_rationale TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN notes TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN jd_fetch_attempts INTEGER DEFAULT 0")
        # Follow-ups widget columns
        _run_migration("ALTER TABLE jobs ADD COLUMN applied_at TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN followup_snooze_until TEXT")
        # task_log + followups are in SCHEMA (CREATE TABLE IF NOT EXISTS) so no migration needed
        # Hunt Targets — Tier A company fields
        _run_migration("ALTER TABLE companies ADD COLUMN tier_a INTEGER DEFAULT 0")
        _run_migration("ALTER TABLE companies ADD COLUMN remote_friendly TEXT")
        _run_migration("ALTER TABLE companies ADD COLUMN nearest_hq TEXT")
        _run_migration("ALTER TABLE companies ADD COLUMN industry_category TEXT")
        _run_migration("ALTER TABLE companies ADD COLUMN gtm_motion TEXT")
        _run_migration("ALTER TABLE companies ADD COLUMN csrevops_setup TEXT")
        _run_migration("ALTER TABLE companies ADD COLUMN gap_hypothesis TEXT")
        _run_migration("ALTER TABLE companies ADD COLUMN gap_hypothesis_date TEXT")
        _run_migration("ALTER TABLE jobs ADD COLUMN match_sections_to_drop_json TEXT")
        # WP-E: full tailored cover letter (structured JSON: recipient/salutation/body/closing)
        _run_migration("ALTER TABLE jobs ADD COLUMN match_cover_letter_json TEXT")
        # WP-L: Discovery match clarity — per-company summary of matching open roles
        _run_migration("ALTER TABLE companies ADD COLUMN match_count INTEGER DEFAULT 0")
        _run_migration("ALTER TABLE companies ADD COLUMN match_best_score REAL")
        _run_migration("ALTER TABLE companies ADD COLUMN matches_refreshed_at TEXT")
        # Discovered jobs were historically inserted without company_id (the hunter
        # and on-demand scans only set the denormalized `company` name). Backfill it
        # so match summaries can count + link existing roles to their company.
        _run_migration(
            "UPDATE jobs SET company_id = ("
            "  SELECT c.id FROM companies c WHERE c.name = jobs.company"
            ") WHERE company_id IS NULL "
            "  AND EXISTS (SELECT 1 FROM companies c WHERE c.name = jobs.company)"
        )


def log_task_event(task_type: str, status: str, message: str = "", entity_name: str = "") -> None:
    """Write a task lifecycle event to task_log. Silently swallows errors — logging must never break the caller."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO task_log (task_type, status, message, entity_name) VALUES (?,?,?,?)",
                (task_type, status, message, entity_name),
            )
            # Keep log bounded — drop anything older than 500 rows
            conn.execute(
                "DELETE FROM task_log WHERE id NOT IN (SELECT id FROM task_log ORDER BY id DESC LIMIT 500)"
            )
    except Exception as e:
        logging.warning(f"[task_log] Failed to log event: {e}")


def log_usage_event(
    method: str,
    route_template: str,
    status: int,
    duration_ms: int,
    is_hx: bool = False,
    actor_id: str | None = None,
) -> None:
    """Persist one request to usage_events. Swallows errors — must never break a request."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_events (actor_id, method, route_template, status, duration_ms, is_hx) "
                "VALUES (?,?,?,?,?,?)",
                (actor_id, method, route_template, status, duration_ms, 1 if is_hx else 0),
            )
    except Exception as e:
        logging.warning(f"[usage_events] Failed to log event: {e}")


def log_error_event(
    request_id: str | None,
    method: str | None,
    route_template: str | None,
    exc_type: str,
    message: str,
    actor_id: str | None = None,
) -> None:
    """Persist one unhandled exception to error_events. Swallows its own errors."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO error_events (request_id, method, route_template, exc_type, message, actor_id) "
                "VALUES (?,?,?,?,?,?)",
                (request_id, method, route_template, exc_type, (message or "")[:1000], actor_id),
            )
    except Exception as e:
        logging.warning(f"[error_events] Failed to log event: {e}")


def prune_observability_tables(retention_days: int = 180) -> None:
    """Delete usage_events / error_events older than retention_days. Called from the daily cron."""
    try:
        cutoff = f"-{int(retention_days)} days"
        with get_db() as conn:
            conn.execute(
                "DELETE FROM usage_events WHERE ts < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM error_events WHERE ts < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
                (cutoff,),
            )
    except Exception as e:
        logging.warning(f"[observability] prune failed: {e}")


def capture_repo_baselines() -> None:
    """Compute and persist repo baselines: db_tables count and test_count.

    These are metrics that change as the codebase evolves. Rather than hand-entering
    them (which rots), we compute them at runtime and upsert to progress_baselines.
    Called once on app startup (app/main.py::web()) and available for /settings/health
    + board metrics. Also seeds the two static, externally-proven baselines
    (index_speedup, sheets_removal) and the milestone rows — both idempotent.
    """
    import subprocess
    from datetime import datetime

    repo_root = Path(__file__).resolve().parent.parent

    try:
        with get_db() as conn:
            # Count CREATE TABLE statements in sqlite_master
            db_tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]

            # Count test_ functions via pytest collection
            try:
                result = subprocess.run(
                    ["pytest", "--co", "-q"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(repo_root),
                )
                # Last line of output is "N tests collected" or similar
                output = result.stdout.strip().split('\n')
                test_count = 0
                for line in reversed(output):
                    if "collected" in line:
                        parts = line.split()
                        if parts and parts[0].isdigit():
                            test_count = int(parts[0])
                        break
            except Exception:
                test_count = 0

            if not test_count:
                # Fallback: grep for def test_ (also covers environments where pytest
                # collection fails, e.g. tests/ absent from a deployed image)
                result = subprocess.run(
                    ["grep", "-rc", "def test_", "tests/"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=str(repo_root),
                )
                test_count = sum(
                    int(line.rsplit(":", 1)[1]) for line in result.stdout.strip().split("\n")
                    if line and line.rsplit(":", 1)[1].isdigit()
                ) if result.stdout else 0

            now = datetime.utcnow().isoformat() + "Z"

            # Upsert db_tables
            conn.execute(
                """INSERT INTO progress_baselines (metric_key, label, value, captured_at, source)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(metric_key) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                ("db_tables", "Database tables", str(db_tables), now, "sqlite_master COUNT(*)")
            )

            # Upsert test_count
            conn.execute(
                """INSERT INTO progress_baselines (metric_key, label, value, captured_at, source)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(metric_key) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                ("test_count", "Test functions", str(test_count), now, "pytest --co -q")
            )

            # Static, externally-proven baselines (spec §1) — INSERT OR IGNORE, never rewritten.
            conn.execute(
                """INSERT OR IGNORE INTO progress_baselines (metric_key, label, value, captured_at, source)
                   VALUES (?, ?, ?, ?, ?)""",
                ("index_speedup", "Query speedup from indexing",
                 "19–232x (bench_indexes.py, 20k/5k)", now, "scripts/bench_indexes.py"),
            )
            conn.execute(
                """INSERT OR IGNORE INTO progress_baselines (metric_key, label, value, captured_at, source)
                   VALUES (?, ?, ?, ?, ?)""",
                ("sheets_removal", "Google Sheets integration removed",
                 "net -1056 LOC / 3 deps", now, "commit d5aeea7"),
            )
    except Exception as e:
        logging.warning(f"[progress_baselines] capture_repo_baselines failed: {e}")


def seed_milestones() -> None:
    """Seed narrative milestones (spec §1) from docs/wins.md. INSERT OR IGNORE,
    idempotent on `name` — safe to call on every app boot."""
    milestones = [
        ("Track B rebuild done-bar reached", "2026-06-22",
         "docs/wins.md — Session 38/39, WP-N performance + WP-G forkability"),
        ("Public repo launched (github.com/jeffsleft/searchops)", "2026-06-24",
         "docs/wins.md — Session 42, trackA-showcase published"),
        ("Case study + README screenshots shipped", "2026-07-01",
         "docs/wins.md — trackA-showcase closed"),
        ("Progress-instrumentation layer shipped", "2026-07-04",
         "docs/wins.md — Progress-instrumentation layer"),
    ]
    try:
        with get_db() as conn:
            for name, completed_on, evidence in milestones:
                conn.execute(
                    """INSERT OR IGNORE INTO milestones (name, completed_on, evidence)
                       VALUES (?, ?, ?)""",
                    (name, completed_on, evidence),
                )
    except Exception as e:
        logging.warning(f"[milestones] seed_milestones failed: {e}")
