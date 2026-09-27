"""Build the portfolio demo's fixed data (demo_data/demo_jobs.json).

Runs each fictional job description in demo_data/demo_jds.json once through the
real scoring engine, against the fictional "Alex Rivera" profile and corpus, and
saves the scored rows. The public demo (app/demo_app.py) loads that fixture at
startup and never calls an LLM itself.

Never reads Jeff's real profile or inventory: both loaders are pinned to the
committed example files for the whole run.

    set -a; . ./.env; set +a
    python scripts/build_demo_data.py          # needs GEMINI_API_KEY; ~2-3 min
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_PATH"] = tempfile.mkstemp(suffix=".db")[1]

import yaml  # noqa: E402

import app.config as config  # noqa: E402
import app.models as models  # noqa: E402
import app.scoring.corpus as corpus  # noqa: E402

config.DATABASE_PATH = models.DATABASE_PATH = os.environ["DATABASE_PATH"]
EXAMPLE_PROFILE = yaml.safe_load((ROOT / "candidate_profile.example.yaml").read_text())
config._load_yaml_profile = lambda: EXAMPLE_PROFILE
corpus.resolve_inventory_path = lambda: (corpus.EXAMPLE_INVENTORY_PATH, True)

# Where each role sits in the demo pipeline, so the board and funnel have a story.
STAGES = {
    "seed_001": "recruiter", "seed_003": "hm_interview", "demo_006": "applied", "demo_007": "researching",
    "demo_009": "outreach", "demo_011": "applied", "seed_004": "discovered", "seed_002": "discovered",
    "demo_008": "i_declined", "demo_010": "dismissed", "seed_005": "discovered",
}
KEEP = ["company", "job_title", "url", "jd_text", "pipeline_stage", "final_score", "deterministic_score",
        "llm_adjustment", "match_score", "adjustment_weights_score", "auto_rejected", "reject_reason",
        "tier", "pros", "cons", "greenfield", "greenfield_rationale", "sector", "tech_stack_detected",
        "recommended_angle", "match_summary", "match_evidence_json", "match_mismatches_json",
        "match_tailored_summary", "match_bullets_json", "match_hooks_json", "match_differentiators_json",
        "role_archetype", "flags", "salary_range", "location", "remote_policy"]


def main() -> None:
    from app.services.scoring_service import score_job_from_text_and_persist

    models.init_db()
    jds = json.loads((ROOT / "demo_data" / "demo_jds.json").read_text())
    out = []
    for jd in jds:
        with models.get_db() as conn:
            job_id = conn.execute(
                "INSERT INTO jobs (company, job_title, url, jd_text, pipeline_stage, date_found) "
                "VALUES (?, ?, ?, ?, 'discovered', date('now'))",
                (jd["company"], jd["job_title"], jd["url"], jd["jd_text"])).lastrowid
        result = score_job_from_text_and_persist(job_id, jd["jd_text"], jd["url"])
        with models.get_db() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        row["pipeline_stage"] = STAGES.get(jd["id"], "discovered")
        # The engine re-parses company/title from the JD; keep the fixture's names.
        row["company"], row["job_title"] = jd["company"], jd["job_title"]
        out.append({k: row.get(k) for k in KEEP if k in row})
        print(f"{jd['company']:22s} {result.get('status'):8s} score={row.get('final_score')} "
              f"L2={row.get('match_score')} rejected={row.get('auto_rejected')}")
        time.sleep(5)  # free-tier pacing
    dest = ROOT / "demo_data" / "demo_jobs.json"
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {len(out)} jobs to {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
