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
    return len(jobs)
