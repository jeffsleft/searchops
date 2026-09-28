"""Build the public demo's database from the committed fixture.

demo_data/demo_jobs.json holds fictional roles scored once by the real engine
against the fictional example candidate (scripts/build_demo_data.py). At demo
startup this writes them into a fresh SQLite file with found dates and stage
history spread over the last few weeks, so the dashboard, pipeline and funnel
look like a search in progress. Nothing here touches the real database.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "demo_data" / "demo_jobs.json"

# Order a role passes through on its way to its fixture stage.
_PATH = ["discovered", "researching", "outreach", "applied", "recruiter", "hm_interview"]


def seed_demo_db() -> int:
    from app.models import get_db, init_db
    from app.pipeline.tracker import STAGES

    init_db()
    jobs = json.loads(FIXTURE.read_text())
    with get_db() as conn:
        if conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
            return 0  # already seeded (container restart with the same /tmp file)
        for i, job in enumerate(jobs):
            cols = [k for k in job if k != "pipeline_stage"]
            days_ago = 3 + i * 2
            target = job["pipeline_stage"]
            # Inserted at its final stage (fixture data, not a stage change), with the
            # history rows below telling how it got there.
            job_id = conn.execute(
                f"INSERT INTO jobs ({', '.join(cols)}, pipeline_stage, date_found, ethics_vetted, discovery_source) "
                f"VALUES ({', '.join('?' * len(cols))}, ?, date('now', ?), 1, 'hunter')",
                [job[c] for c in cols] + [target, f"-{days_ago} days"]).lastrowid
            steps = _PATH[1:_PATH.index(target) + 1] if target in _PATH else [target]
            for n, stage in enumerate(s for s in steps if s in STAGES and s != "discovered"):
                note = "Decline reason: Requires in-office" if stage == "i_declined" else None
                conn.execute(
                    "INSERT INTO pipeline_history (job_id, from_stage, to_stage, changed_at, notes, changed_by) "
                    "VALUES (?, ?, ?, datetime('now', ?), ?, 'demo')",
                    (job_id, "discovered" if n == 0 else steps[n - 1], stage, f"-{max(days_ago - 2 - n, 0)} days", note))
        _seed_history(conn)
    return len(jobs)


# ---------------------------------------------------------------------------
# Fictional history for Watched Companies, Settings, Progress and Health.
# Deterministic (fixed seed) so every visitor sees the same demo. Every
# company and role here is invented; board URLs point at no real company.
# ---------------------------------------------------------------------------
import random  # noqa: E402

_COMPANIES = [  # name, sector, funding stage, headcount, gtm motion
    ("Forgeline", "Dev Tools", "Series D", 420, "PLG moving to enterprise sales"),
    ("Sentora", "Developer Security", "Series C", 310, "Sales-led, usage-based"),
    ("Tessellate", "AEC Software", "Series C", 380, "Sales-led"),
    ("Brightline Freight", "Logistics SaaS", "Series B", 150, "Founder-led to repeatable"),
    ("Orbitwise", "Observability", "Series C", 300, "PLG + enterprise"),
    ("Cadence Ledger", "Fintech", "Series B", 180, "Sales-led"),
    ("MidCo Analytics", "BI & Analytics", "Series B", 220, "Sales-led"),
    ("Quarrystone Analytics", "Data Infrastructure", "Series C", 260, "PLG"),
    ("Pinewell Payments", "Fintech", "Series D", 520, "Enterprise sales"),
    ("Harborlight AI", "AI-native SaaS", "Series B", 140, "PLG"),
    ("Northgate Logistics", "Logistics SaaS", "Series C", 290, "Sales-led"),
    ("Vantage Loop", "Customer Success Platform", "Series B", 160, "Sales-led"),
]
_BG_COMPANIES = ["Quarrystone Analytics", "Pinewell Payments", "Harborlight AI", "Northgate Logistics",
                 "Vantage Loop", "Copperfield Labs", "Meridian Stack", "Tallgrass Health Tech",
                 "Lattice Row", "Bluefin Systems", "Oakmere Software", "Sable Metrics"]
_BG_TITLES = ["Director, Revenue Operations", "Head of GTM Operations", "Director of Business Operations",
              "Senior Manager, Sales Operations", "VP, Customer Operations", "Director, CS Operations",
              "Head of Strategy & Operations", "Director, GTM Strategy"]
_TITLE_FILTERS = {
    "positive": ["Revenue Operations", "RevOps", "GTM Operations", "GTM Strategy", "Sales Operations",
                 "Customer Success Operations", "Business Operations", "Strategy & Operations"],
    "negative": ["Intern", "Analyst", "Coordinator"],
}
_HQS = ["San Francisco", "Denver", "Austin", "Seattle", "New York", "Remote-first"]

# Researched gap hypotheses (the "Researched" tab). Fictional, like the companies.
_GAPS = {
    "Forgeline": "Moving upmarket without a forecast process; CS and Sales run separate renewal numbers.",
    "Sentora": "Usage-based pricing shipped before billing ops caught up; expansion is tracked in spreadsheets.",
    "Tessellate": "Three acquired products on three CRMs; no single view of the customer.",
    "Brightline Freight": "Founder still owns the pipeline review; first ops hire will build it from scratch.",
    "Orbitwise": "PLG signups convert, but there is no handoff from product signals to the sales team.",
    "Harborlight AI": "Hiring its first GTM leader; churn is untracked below the top 20 accounts.",
}
_ETHICS_REASONS = ["Deceptive sales practices", "Regulatory enforcement history", "Harmful product category"]


def _seed_history(conn) -> None:
    rng = random.Random(7)
    ids = {r[1]: r[0] for r in conn.execute("SELECT id, company FROM jobs")}

    # Watched companies, linked to the scored roles they posted.
    for name, sector, stage, heads, gtm in _COMPANIES:
        slug = name.lower().replace(" ", "")
        co_id = conn.execute(
            "INSERT INTO companies (name, website, sector, industry_category, funding_stage, headcount_estimate, "
            "tier_a, gap_hypothesis, gap_hypothesis_date, hunt_enabled, careers_url, ats_type, ats_handle, last_scanned, last_listed, zero_scans, "
            "nearest_hq, remote_friendly, gtm_motion, source, status, date_added) VALUES "
            "(?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?, 'greenhouse', ?, datetime('now', '-6 hours'), ?, 0, ?, 'Yes', ?, "
            "'demo', 'Watchlist', date('now', '-60 days'))",
            (name, f"https://{slug}.example.com", sector, sector, stage, heads,
             _GAPS.get(name), "2026-09-01" if name in _GAPS else None, f"https://boards.greenhouse.io/{slug}-demo", f"{slug}-demo", rng.randint(18, 240), rng.choice(_HQS), gtm)).lastrowid
        conn.execute("UPDATE jobs SET company_id = ? WHERE company = ?", (co_id, name))

    # Older roles, already closed out, so the funnel and metrics have volume.
    outcomes_by_stage = {"they_declined": "rejected_me", "i_declined": "rejected_them"}
    plan = (["job_listing_closed"] * 9 + ["dismissed"] * 7 + ["they_declined"] * 4
            + ["i_declined"] * 2 + ["duplicate"])
    for n, stage in enumerate(plan):
        company = _BG_COMPANIES[n % len(_BG_COMPANIES)]
        days = rng.randint(20, 60)
        applied = stage in ("they_declined", "i_declined") or (stage == "job_listing_closed" and n % 3 == 0)
        score = round(rng.uniform(4.0, 8.4) if applied else rng.uniform(1.5, 6.5), 1)
        job_id = conn.execute(
            "INSERT INTO jobs (company, job_title, url, pipeline_stage, final_score, auto_rejected, ethics_vetted, "
            "discovery_source, date_found, applied_at) VALUES (?, ?, ?, ?, ?, 0, 1, 'hunter', date('now', ?), ?)",
            (company, rng.choice(_BG_TITLES), f"https://example.com/demo-role-{n}", stage, score,
             f"-{days} days", None)).lastrowid
        if applied:
            conn.execute("UPDATE jobs SET applied_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) WHERE id = ?", (f"-{days - 3} days", job_id))
            conn.execute("INSERT INTO application_outcomes (job_id, outcome, recorded_at) VALUES "
                         "(?, 'applied', strftime('%Y-%m-%dT%H:%M:%S', 'now', ?))", (job_id, f"-{days - 3} days"))
            if n % 2 == 0:
                conn.execute("INSERT INTO application_outcomes (job_id, outcome, recorded_at) VALUES "
                             "(?, 'phone_screen', strftime('%Y-%m-%dT%H:%M:%S', 'now', ?))", (job_id, f"-{days - 10} days"))
            final = outcomes_by_stage.get(stage, "ghosted")
            conn.execute("INSERT INTO application_outcomes (job_id, outcome, recorded_at) VALUES "
                         "(?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now', ?))", (job_id, final, f"-{max(days - 16, 1)} days"))
        conn.execute("INSERT INTO score_history (job_id, final_score, scored_at) VALUES (?, ?, date('now', ?))",
                     (job_id, score, f"-{days} days"))
        steps = ["researching", "applied", stage] if applied else [stage]
        for k, to_stage in enumerate(steps):
            conn.execute("INSERT INTO pipeline_history (job_id, from_stage, to_stage, changed_at, changed_by) "
                         "VALUES (?, ?, ?, datetime('now', ?), 'demo')",
                         (job_id, "discovered" if k == 0 else steps[k - 1], to_stage, f"-{max(days - 2 - 6 * k, 1)} days"))

    # The scored showcase roles that went out as applications.
    progress = {"Tessellate": ["applied"], "Cadence Ledger": ["applied"], "Orbitwise": ["applied"],
                "Forgeline": ["applied", "phone_screen"], "Sentora": ["applied", "phone_screen", "interview"]}
    for company, steps in progress.items():
        job_id = ids.get(company)
        if job_id is None:
            continue
        conn.execute("UPDATE jobs SET applied_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-12 days') WHERE id = ?", (job_id,))
        for k, outcome in enumerate(steps):
            conn.execute("INSERT INTO application_outcomes (job_id, outcome, recorded_at) VALUES "
                         "(?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now', ?))", (job_id, outcome, f"-{12 - 4 * k} days"))
    for job_id, score in conn.execute("SELECT id, final_score FROM jobs WHERE final_score IS NOT NULL "
                                      "AND id NOT IN (SELECT job_id FROM score_history)").fetchall():
        conn.execute("INSERT INTO score_history (job_id, final_score, scored_at) VALUES (?, ?, date('now', '-5 days'))",
                     (job_id, score))

    # Match counts shown on Watched Companies.
    conn.execute("UPDATE companies SET match_count = (SELECT COUNT(*) FROM jobs j WHERE j.company_id = companies.id), "
                 "match_best_score = (SELECT MAX(final_score) FROM jobs j WHERE j.company_id = companies.id), "
                 "matches_refreshed_at = datetime('now', '-6 hours')")

    # Background work and request history for Health.
    for d in range(30, 0, -1):
        found = rng.randint(0, 4)
        conn.execute("INSERT INTO task_log (task_type, status, message, logged_at) VALUES "
                     "('discovery_scan', 'completed', ?, datetime('now', ?))",
                     (f"Scanned 12, found {found}, auto-scored {found}, errors 0", f"-{d} days"))
        conn.execute("INSERT INTO task_log (task_type, status, message, logged_at) VALUES "
                     "('close_dead_listings', 'completed', ?, datetime('now', ?, '+6 hours'))",
                     (f"closed {rng.randint(0, 2)} of {rng.randint(8, 20)}; skipped 0 boards", f"-{d} days"))
    conn.execute("INSERT INTO task_log (task_type, status, message, logged_at) VALUES "
                 "('discovery_scan', 'completed', 'Scanned 12, found 1, auto-scored 1, errors 0', "
                 "datetime('now', '-3 hours'))")
    routes = [("GET", "/", 12), ("GET", "/pipeline", 8), ("GET", "/job/{job_id:int}", 22), ("GET", "/discovered", 9),
              ("POST", "/job/{job_id:int}/stage", 6), ("GET", "/targets", 5), ("POST", "/job/score", 3200)]
    for d in range(14, 0, -1):
        for _ in range(rng.randint(40, 90)):
            method, route, base_ms = rng.choice(routes)
            conn.execute("INSERT INTO usage_events (ts, method, route_template, status, duration_ms) VALUES "
                         "(strftime('%Y-%m-%dT%H:%M:%S', 'now', ?, ?), ?, ?, 200, ?)",
                         (f"-{d} days", f"+{rng.randint(0, 50000)} seconds", method, route,
                          int(base_ms * rng.uniform(0.6, 2.2))))

    # Month-end funnel snapshots for the Progress trend.
    for months, (apps, screens, interviews) in enumerate([(3, 1, 0), (5, 3, 1), (6, 5, 1)]):
        conn.execute("INSERT OR IGNORE INTO progress_snapshots (snapshot_date, jobs_scored, companies_covered, "
                     "apps_sent, phone_screens, interviews, offers) VALUES "
                     "(date('now', 'start of month', ?), ?, 12, ?, ?, ?, 0)",
                     (f"-{2 - months} months", 14 + months * 9, apps, screens, interviews))

    # A generic Settings baseline (the real app seeds these from personal config).
    for kind, values in _TITLE_FILTERS.items():
        for v in values:
            conn.execute("INSERT OR IGNORE INTO title_filters (filter_type, value) VALUES (?, ?)", (kind, v))
    for reason in _ETHICS_REASONS:
        conn.execute("INSERT OR IGNORE INTO ethics_reasons (reason) VALUES (?)", (reason,))
