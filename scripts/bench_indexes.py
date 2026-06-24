"""WP-N performance benchmark — quantifies the DB-index win offline.

Seeds a throwaway SQLite DB with realistic-scale synthetic data, then times the
app's hot read queries with and without the WP-N indexes. Pure stdlib; never
touches the real recruiting.db or the network.

Run:  python scripts/bench_indexes.py [n_jobs] [n_companies]
"""
import os
import sqlite3
import sys
import tempfile
import time

# The exact index set added by WP-N (kept in sync with app/models.py SCHEMA).
WPN_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_stage ON jobs(pipeline_stage)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_active_score ON jobs(auto_rejected, final_score)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_date_found ON jobs(date_found)",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_history_job ON pipeline_history(job_id, changed_at)",
    "CREATE INDEX IF NOT EXISTS idx_score_history_job ON score_history(job_id, scored_at)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_job ON contacts(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_questions_job ON questions(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_transcripts_job ON transcripts(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_briefs_job ON strategy_briefs(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_interviews_job ON interviews(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status)",
    "CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name)",
]

# (label, sql, params) — mirrors the real route queries.
QUERIES = [
    ("dashboard live jobs",
     "SELECT * FROM jobs WHERE auto_rejected = 0 AND final_score IS NOT NULL ORDER BY final_score DESC", ()),
    ("pipeline jobs",
     "SELECT * FROM jobs WHERE auto_rejected = 0 ORDER BY final_score DESC", ()),
    ("discovered count",
     "SELECT COUNT(*) FROM jobs WHERE pipeline_stage = 'discovered'", ()),
    ("new since last (7d)",
     "SELECT COUNT(*) FROM jobs WHERE date_found >= date('now','-7 days')", ()),
    ("target_detail jobs by company",
     "SELECT id FROM jobs WHERE company_id = ? AND COALESCE(discovery_source,'') LIKE 'hunt%' "
     "ORDER BY COALESCE(final_score, lightweight_score) DESC", (123,)),
    ("companies by status",
     "SELECT * FROM companies WHERE status = 'Watchlist' ORDER BY fit_score DESC", ()),
    ("companies status counts",
     "SELECT status, COUNT(*) FROM companies GROUP BY status", ()),
    ("pipeline_history by job",
     "SELECT * FROM pipeline_history WHERE job_id = ? ORDER BY changed_at", (123,)),
    ("score_history by job",
     "SELECT * FROM score_history WHERE job_id = ? ORDER BY scored_at", (123,)),
    ("company name lookup",
     "SELECT id FROM companies WHERE name = ?", ("Company 2500",)),
]

SCHEMA = """
CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT, status TEXT, fit_score REAL,
    tier_a INTEGER DEFAULT 0, careers_url TEXT, hunt_enabled INTEGER DEFAULT 0,
    last_scanned TEXT, gap_hypothesis TEXT, match_count INTEGER DEFAULT 0);
CREATE TABLE jobs (id INTEGER PRIMARY KEY, company_id INTEGER, company TEXT, job_title TEXT,
    date_found DATE, final_score REAL, lightweight_score REAL, match_score REAL,
    auto_rejected INTEGER DEFAULT 0, pipeline_stage TEXT, discovery_source TEXT, jd_text TEXT,
    role_archetype TEXT);
CREATE TABLE pipeline_history (id INTEGER PRIMARY KEY, job_id INTEGER, to_stage TEXT, changed_at TEXT);
CREATE TABLE score_history (id INTEGER PRIMARY KEY, job_id INTEGER, final_score REAL, scored_at TEXT);
CREATE TABLE contacts (id INTEGER PRIMARY KEY, job_id INTEGER, company_id INTEGER, name TEXT);
CREATE TABLE questions (id INTEGER PRIMARY KEY, job_id INTEGER, question TEXT);
CREATE TABLE transcripts (id INTEGER PRIMARY KEY, job_id INTEGER, raw_transcript TEXT);
CREATE TABLE strategy_briefs (id INTEGER PRIMARY KEY, job_id INTEGER, content TEXT);
CREATE TABLE interviews (id INTEGER PRIMARY KEY, job_id INTEGER, transcript_id INTEGER, round TEXT);
"""

STAGES = ["discovered", "identified", "evaluated", "recruiter", "hm_interview",
          "panel", "final_offer", "accepted", "they_declined", "i_declined"]


def seed(conn, n_jobs, n_companies):
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO companies (id,name,status,fit_score,tier_a,careers_url,hunt_enabled,match_count) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(i, f"Company {i}", ["Watchlist", "Researching", "Outreach", "Active", "Closed"][i % 5],
          (i % 100) / 10.0, i % 2, f"https://x/{i}/careers" if i % 3 else None, i % 2, i % 4)
         for i in range(n_companies)],
    )
    conn.executemany(
        "INSERT INTO jobs (id,company_id,company,job_title,date_found,final_score,lightweight_score,"
        "match_score,auto_rejected,pipeline_stage,discovery_source,jd_text) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i, i % n_companies, f"Company {i % n_companies}", "RevOps Lead",
          f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", (i % 100) / 10.0, (i % 90) / 10.0,
          (i % 80) / 10.0 if i % 3 else None, 1 if i % 4 == 0 else 0,
          STAGES[i % len(STAGES)], "hunt" if i % 2 else "manual", "JD body text")
         for i in range(n_jobs)],
    )
    conn.executemany("INSERT INTO pipeline_history (job_id,to_stage,changed_at) VALUES (?,?,?)",
                     [(i % n_jobs, STAGES[i % len(STAGES)], f"2026-06-{1 + i % 28:02d}") for i in range(n_jobs * 3)])
    conn.executemany("INSERT INTO score_history (job_id,final_score,scored_at) VALUES (?,?,?)",
                     [(i % n_jobs, (i % 100) / 10.0, f"2026-06-{1 + i % 28:02d}") for i in range(n_jobs * 2)])
    conn.commit()


def timeit(conn, sql, params, reps=200):
    best = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        conn.execute(sql, params).fetchall()
        best = min(best, time.perf_counter() - t)
    return best * 1000.0  # ms


def main():
    n_jobs = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    n_companies = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    path = os.path.join(tempfile.mkdtemp(), "bench.db")
    conn = sqlite3.connect(path)
    seed(conn, n_jobs, n_companies)
    print(f"Seeded {n_jobs:,} jobs / {n_companies:,} companies\n")

    before = {label: timeit(conn, sql, p) for label, sql, p in QUERIES}
    for stmt in WPN_INDEXES:
        conn.execute(stmt)
    conn.execute("ANALYZE")
    conn.commit()
    after = {label: timeit(conn, sql, p) for label, sql, p in QUERIES}

    print(f"{'query':<34}{'before':>10}{'after':>10}{'speedup':>10}")
    print("-" * 64)
    for label, _, _ in QUERIES:
        b, a = before[label], after[label]
        sp = f"{b / a:.1f}x" if a > 0 else "-"
        print(f"{label:<34}{b:>9.3f}m{a:>9.3f}m{sp:>10}")
    conn.close()


if __name__ == "__main__":
    main()
