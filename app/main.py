import logging
from pathlib import Path

import modal
from app.models import init_db

app = modal.App("recruiting-engine")

volume = modal.Volume.from_name("recruiting-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements.txt")
    .add_local_python_source("app")
    .add_local_dir("app/static", remote_path="/root/app/static")
    .add_local_dir("app/templates", remote_path="/root/app/templates")
    # WP-F: voice-guide YAMLs aren't .py, so add_local_python_source skips them.
    .add_local_dir("app/voice/constraints", remote_path="/root/app/voice/constraints")
    # capture_repo_baselines() shells out to `pytest --co -q` for a live test_count;
    # tests/ isn't picked up by add_local_python_source (that only mounts app/).
    .add_local_dir("tests", remote_path="/root/tests")
)

# Personal config — candidate profile + hunt targets. Untracked from git (WP-A), so
# absent in a fresh clone. Mount when present; otherwise fall back (profile → {} via
# app/config.py; hunt targets → seed skipped) so the image build and app still work.
_ROOT = Path(__file__).resolve().parent.parent
for _cfg, _remote in (
    ("candidate_profile.yaml", "/root/candidate_profile.yaml"),
    ("app/discovery/hunt_targets.yaml", "/root/app/discovery/hunt_targets.yaml"),
):
    if (_ROOT / _cfg).exists():
        image = image.add_local_file(_cfg, _remote)
    else:
        print(f"[main] WARNING: {_cfg} not found — using fallback config.")

# Layer 2 corpus — Accomplishments Inventory. Optional during the gap before Cowork
# delivers the docx. When present, mounted at /root/data/Accomplishments_Inventory.docx
# so app/scoring/corpus.py finds it via ROOT/"data"/"Accomplishments_Inventory.docx".
_INVENTORY = Path(__file__).resolve().parent.parent / "data" / "Accomplishments_Inventory.docx"
if _INVENTORY.exists():
    image = image.add_local_file(
        str(_INVENTORY), "/root/data/Accomplishments_Inventory.docx"
    )
else:
    print(f"[main] WARNING: {_INVENTORY} not found — Layer 2 (match) will return 0.0 until it is added.")

# Resume template for tailored PDF assembler. Optional — route degrades gracefully if absent.
_RESUME = Path(__file__).resolve().parent.parent / "data" / "resume.docx"
if _RESUME.exists():
    image = image.add_local_file(str(_RESUME), "/root/data/resume.docx")
else:
    print(f"[main] WARNING: {_RESUME} not found — /job/{{id}}/resume will return a parse error.")

recruiting_secrets = modal.Secret.from_name("recruiting-secrets")
anthropic_secret = modal.Secret.from_name("anthropic-key")


# Scheduler runs at midnight, 6am, noon, 6pm UTC.
@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    schedule=modal.Cron("0 0,6,12,18 * * *"),
    timeout=120,
)
def scheduler():
    """
    Single cron entry — runs at midnight, 6am, noon, 6pm UTC.
    - 6am UTC: run automated job discovery scan.
    - Monday 8am PT: also send weekly Slack digest.
    """
    from datetime import datetime, timezone

    from app.observability import configure_logging
    configure_logging()
    init_db()

    # Also research a batch of 5 outreach targets if they exist
    from app.models import get_db
    with get_db() as conn:
        rows = conn.execute("SELECT id FROM companies WHERE research_date IS NULL LIMIT 5").fetchall()
    if rows:
        print(f"[scheduler] Triggering background research for {len(rows)} outreach targets.")
        batch_research_companies.spawn([r["id"] for r in rows])

    now_utc = datetime.now(timezone.utc)

    # Prune observability tables once daily (midnight UTC tick) to bound Volume growth.
    if now_utc.hour == 0:
        from app.models import prune_observability_tables
        from app.config import USAGE_RETENTION_DAYS
        prune_observability_tables(USAGE_RETENTION_DAYS)

    # Run discovery scan daily at 6am UTC. Offloaded to run_discovery_scan_remote
    # via .spawn() (its own container, timeout=900) rather than run inline: the scan
    # loops every hunt-enabled company through network fetches + LLM search dorks and
    # routinely exceeds this cron's timeout. The scan sends the score-aware Slack
    # digest ("N new roles ≥ 8.0 today") when it finds new roles.
    if now_utc.hour == 6:
        run_discovery_scan_remote.spawn()

    # Send the weekly Slack digest once a week at the Monday 18:00 UTC tick
    # (== 11am PDT / 10am PST). Gated to an actual cron tick: the previous
    # "Monday 8am PT" gate never fired, since the cron only ticks at
    # 0/6/12/18 UTC and 8am PT lands on none of them.
    if now_utc.weekday() == 0 and now_utc.hour == 18:
        from app.notifications.slack import send_weekly_digest
        send_weekly_digest()

    # Weekly DB backup — Sunday 00:00 UTC tick. Folded into this cron (rather than
    # its own scheduled function) because Modal caps the workspace at 5 scheduled
    # functions. backup_database() Slack-pings on its own failures; catch here so a
    # backup error never aborts the rest of the scheduler run.
    if now_utc.weekday() == 6 and now_utc.hour == 0:
        from app.maintenance.db_backup import backup_database
        try:
            backup_database()
        except Exception as e:
            print(f"[scheduler] DB backup failed: {e}")

    # Monthly progress snapshot — 1st-of-month, 00:00 UTC tick. Also folded into this
    # cron rather than its own scheduled function (same 5-cron-cap constraint as the
    # backup above). snapshot_progress() is idempotent on snapshot_date (INSERT OR
    # REPLACE), so it's safe even if this branch somehow fires more than once in a day.
    if now_utc.day == 1 and now_utc.hour == 0:
        from app.crons.progress import snapshot_progress
        try:
            result = snapshot_progress()
            print(f"[scheduler] Progress snapshot captured for {result['snapshot_date']}")
        except Exception as e:
            print(f"[scheduler] Progress snapshot failed: {e}")


# Not scheduled (Modal's 5-scheduled-function cap is full) — the weekly run is driven
# by scheduler() above. Kept as a manually-invokable function for on-demand backups
# and verification: `modal run app/main.py::backup_db`.
@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=300,
)
def backup_db():
    """Snapshot recruiting.db → /data/backups (rotates last 8)."""
    init_db()
    from app.maintenance.db_backup import backup_database
    backup_database()


_STUB_JOBS_QUERY = """
    SELECT id, company, job_title, COALESCE(LENGTH(jd_text), 0) AS jd_len,
           auto_rejected, pipeline_stage, final_score, source_url
    FROM jobs
    WHERE COALESCE(company, '') IN ('', 'Unknown')
       OR COALESCE(job_title, '') IN ('', 'Untitled role')
       OR (pipeline_stage = 'identified' AND final_score IS NULL)
    ORDER BY id
"""


# Read-only. Classifies Discovered-panel stub rows (blank/Unknown company or title,
# or never-scored 'identified' rows) into Bucket 1 (jd_text already saved — just
# needs the existing rescore logic re-run) vs Bucket 2 (no usable jd_text — needs
# the JD sourced before anything can be scored). Invoke: `modal run app/main.py::diagnose_stubs`
@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=120,
)
def diagnose_stubs():
    init_db()
    from app.models import get_db

    with get_db() as conn:
        rows = conn.execute(_STUB_JOBS_QUERY).fetchall()

    bucket1 = 0
    bucket2 = 0
    for row in rows:
        r = dict(row)
        bucket = "BUCKET_1_rescore_ready" if r["jd_len"] >= 100 else "BUCKET_2_needs_jd"
        bucket1 += bucket == "BUCKET_1_rescore_ready"
        bucket2 += bucket == "BUCKET_2_needs_jd"
        print(
            f"[diagnose] id={r['id']} company='{r['company']}' title='{r['job_title']}' "
            f"jd_len={r['jd_len']} auto_rejected={r['auto_rejected']} stage={r['pipeline_stage']} "
            f"score={r['final_score']} url={r['source_url']} -> {bucket}"
        )

    print(f"[diagnose] Total stubs: {len(rows)} | Bucket 1 (rescore-ready): {bucket1} | Bucket 2 (needs JD): {bucket2}")


# Re-scores Bucket 1 stub jobs only (jd_text already saved, >= 100 chars) using the
# same score_job_from_text_and_persist() call the existing /job/{id}/rescore route
# uses. Never touches Bucket 2 rows (no JD text) — the Python filter below skips
# them before any scoring call, and the service itself also hard-fails on short
# jd_text. Invoke: `modal run app/main.py::remediate_bucket1`
@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=600,
)
def remediate_bucket1():
    init_db()
    from app.models import get_db
    from app.services.scoring_service import score_job_from_text_and_persist

    with get_db() as conn:
        rows = conn.execute(_STUB_JOBS_QUERY).fetchall()

    attempted = 0
    ok = 0
    errors = 0
    for row in rows:
        r = dict(row)
        if r["jd_len"] < 100:
            continue

        attempted += 1
        with get_db() as conn:
            jd_row = conn.execute("SELECT jd_text FROM jobs WHERE id = ?", (r["id"],)).fetchone()
        jd_text = jd_row["jd_text"]

        result = score_job_from_text_and_persist(r["id"], jd_text, transition_stage=False)
        if result["status"] == "error":
            errors += 1
            print(f"[remediate] id={r['id']} -> ERROR {result.get('error')}")
        else:
            ok += 1
            with get_db() as conn:
                new_row = conn.execute("SELECT company FROM jobs WHERE id = ?", (r["id"],)).fetchone()
            print(
                f"[remediate] id={r['id']} -> OK score={result.get('score')} "
                f"company='{new_row['company']}' (was '{r['company']}')"
            )

    print(f"[remediate] Done: {attempted} attempted, {ok} ok, {errors} errors.")


# One-time backlog rescore under the post-WP-J engine (2026-07-08). Targets jobs
# scored before the 2026-06-22 overhaul (no score_history row since then), skipping
# auto-rejects and jobs without usable JD text. Appends score_history so old-engine
# and new-engine scores stay auditable. Invoke:
#   modal run app/main.py::rescore_stale --dry-run   (list targets, no LLM calls)
#   modal run app/main.py::rescore_stale             (real run, ~5s pacing per job)
_STALE_SCORE_QUERY = """
    SELECT j.id, j.company, j.job_title, j.final_score,
           LENGTH(COALESCE(j.jd_text, '')) AS jd_len
    FROM jobs j
    WHERE j.final_score IS NOT NULL
      AND j.auto_rejected = 0
      AND LENGTH(COALESCE(j.jd_text, '')) >= 100
      AND NOT EXISTS (
          SELECT 1 FROM score_history h
          WHERE h.job_id = j.id AND date(h.scored_at) >= '2026-06-22'
      )
    ORDER BY j.id
"""


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=3600,
)
def rescore_stale(dry_run: bool = False):
    import time

    init_db()
    from app.models import get_db
    from app.services.scoring_service import score_job_from_text_and_persist

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(_STALE_SCORE_QUERY).fetchall()]

    print(f"[rescore_stale] {len(rows)} stale-scored jobs targeted (dry_run={dry_run})")
    if dry_run:
        for r in rows:
            print(f"[rescore_stale] id={r['id']} '{r['company']}' — '{r['job_title']}' old={r['final_score']}")
        return

    ok = errors = 0
    for r in rows:
        with get_db() as conn:
            jd_row = conn.execute("SELECT jd_text FROM jobs WHERE id = ?", (r["id"],)).fetchone()

        result = score_job_from_text_and_persist(r["id"], jd_row["jd_text"], transition_stage=False)
        if result.get("status") == "success":
            ok += 1
            with get_db() as conn:
                new = conn.execute(
                    "SELECT final_score, deterministic_score, llm_adjustment, match_score,"
                    " adjustment_weights_score FROM jobs WHERE id = ?", (r["id"],)).fetchone()
                conn.execute(
                    """INSERT INTO score_history
                       (job_id, final_score, deterministic_score, llm_adjustment,
                        match_score, adjustment_weights_score)
                       VALUES (?,?,?,?,?,?)""",
                    (r["id"], new["final_score"], new["deterministic_score"],
                     new["llm_adjustment"], new["match_score"], new["adjustment_weights_score"]),
                )
            print(f"[rescore_stale] id={r['id']} '{r['company']}' {r['final_score']} -> {result.get('score')}")
        else:
            errors += 1
            print(f"[rescore_stale] id={r['id']} '{r['company']}' -> ERROR {result.get('error')}")

        time.sleep(5)  # AI_RULES §1 pacing between LLM calls

    print(f"[rescore_stale] Done: {ok} rescored, {errors} errors of {len(rows)} targeted.")
    volume.commit()


# One-off admin fix (W1-REDACT, 2026-07-14 plan): Tebra (id 140) has an application
# sent but never got its applied_at stamped, so KR1 and the calibration count
# undercount it. Refuses to overwrite a row that already has applied_at set — this
# is a single deliberate backfill, not a general-purpose field editor.
# Invoke via the deployed function only (never `modal run` — Session 47 lesson),
# off the 0/6/12/18 UTC cron ticks: dry_run first, then dry_run=False to commit.
@app.function(
    image=image,
    secrets=[recruiting_secrets],
    volumes={"/data": volume},
    timeout=60,
)
def stamp_applied_at(job_id: int, applied_date: str, dry_run: bool = True):
    init_db()
    from app.models import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, company, job_title, applied_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            print(f"[stamp_applied_at] no job with id={job_id} — nothing done")
            return
        if row["applied_at"]:
            print(
                f"[stamp_applied_at] id={job_id} '{row['company']}' already has "
                f"applied_at={row['applied_at']} — refusing to overwrite"
            )
            return

        print(
            f"[stamp_applied_at] id={job_id} '{row['company']}' — '{row['job_title']}' "
            f"-> applied_at={applied_date} (dry_run={dry_run})"
        )
        if dry_run:
            return

        conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (applied_date, job_id))

    volume.commit()
    print(f"[stamp_applied_at] id={job_id} committed.")


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=300,
)
def research_one_company_task(co_id: int):
    """Worker task to research a single company."""
    import json
    from datetime import date
    from app.models import get_db, log_task_event
    from app.scoring.research import research_company, assess_company_fit

    with get_db() as conn:
        r = conn.execute("SELECT name, funding_stage FROM companies WHERE id = ?", (co_id,)).fetchone()
    if not r:
        return False

    name = r["name"]
    log_task_event("research", "started", f"Researching {name}", name)
    try:
        print(f"[worker] Starting research for: {name}")
        research = research_company(name)
        fit = assess_company_fit(name, research)

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
                 research.get("funding_stage", r["funding_stage"] or ""), co_id),
            )
        log_task_event("research", "completed", f"Research done — fit={fit.get('fit_score','?')}", name)
        print(f"[worker] Success: {name}")
        return True
    except Exception as e:
        log_task_event("research", "failed", str(e)[:200], name)
        logging.error("[worker] Failed for '%s': %s", name, e)
        return False


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=600,
)
def batch_research_companies(company_ids: list[int]):
    """Orchestrator to run multiple research tasks in parallel."""
    from app.models import log_task_event
    n = len(company_ids)
    log_task_event("batch_research", "started", f"Queued {n} companies for parallel research")
    print(f"[batch] Spawning research for {n} companies in parallel.")
    results = list(research_one_company_task.map(company_ids))
    done = sum(1 for r in results if r)
    status = "completed" if done == n else "partial"
    log_task_event("batch_research", status, f"Finished {done}/{n} successfully")
    print(f"[batch] Completed {done}/{n} successfully.")
    return done


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=1800,
)
def backfill_legacy_research(only_name: str | None = None):
    """Re-research companies whose research_json predates the fit/need-justification feature.

    Targets rows that have been researched (research_date IS NOT NULL) but whose JSON
    lacks the fit_justification field. Optionally restrict to a single company via
    `only_name` for spot-checking.

    Uses the existing research_one_company_task worker so the heavy LLM calls run in
    parallel containers, not sequentially in this orchestrator.
    """
    import json
    from app.models import init_db, get_db
    init_db()

    with get_db() as conn:
        if only_name:
            rows = conn.execute(
                "SELECT id, name, research_json FROM companies WHERE name = ?",
                (only_name,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, research_json FROM companies WHERE research_date IS NOT NULL"
            ).fetchall()

    legacy_ids: list[int] = []
    for r in rows:
        try:
            data = json.loads(r["research_json"]) if r["research_json"] else {}
        except json.JSONDecodeError:
            data = {}
        if not data.get("fit_justification"):
            legacy_ids.append(r["id"])
            print(f"[backfill] queued: {r['name']} (id={r['id']})")

    if not legacy_ids:
        print("[backfill] nothing to do — all researched companies already have justifications")
        return {"queued": 0, "done": 0}

    print(f"[backfill] re-researching {len(legacy_ids)} companies in parallel")
    results = list(research_one_company_task.map(legacy_ids))
    done = sum(1 for r in results if r)
    print(f"[backfill] done: {done}/{len(legacy_ids)} successful")
    return {"queued": len(legacy_ids), "done": done}


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=900,
)
def run_discovery_scan_remote():
    """Manually trigger run_discovery_scan against the Volume-backed DB.

    Useful right after seeding hunt_targets, or for ad-hoc debugging
    without waiting for the 6am UTC scheduler tick.
    """
    from app.models import init_db
    from app.discovery.hunter import run_discovery_scan
    init_db()
    stats = run_discovery_scan()
    print(f"[discovery] Done: {stats}")
    return stats


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=120,
)
def seed_hunt_targets_remote():
    """Seed companies table from app/discovery/hunt_targets.yaml against the Volume-backed DB.

    Idempotent: existing companies (matched by name) are updated with hunt_enabled=1
    and refreshed careers_url/ats fields. New companies are inserted with source='hunter_seed'.
    """
    import yaml
    from datetime import date
    from pathlib import Path
    from app.models import get_db, init_db
    from app.discovery.ats_clients import detect_ats

    init_db()
    config_path = Path("/root/app/discovery/hunt_targets.yaml")
    if not config_path.exists():
        # Fall back to repo-relative path inside the image
        config_path = Path(__file__).parent / "discovery" / "hunt_targets.yaml"
    if not config_path.exists():
        print(f"[seed] Config not found at {config_path}")
        return 0

    with open(config_path) as f:
        config = yaml.safe_load(f)

    today = str(date.today())
    inserted, updated = 0, 0

    with get_db() as conn:
        for group in config.get("tracked_companies", []):
            category = group.get("category", "Unknown")
            for company in group.get("companies", []):
                name = company.get("name")
                url = company.get("url")
                if not name or not url:
                    continue
                ats_type, ats_handle = detect_ats(url)
                existing = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE companies SET
                           careers_url = ?, ats_type = ?, ats_handle = ?,
                           sector = ?, hunt_enabled = 1, updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (url, ats_type, ats_handle, category, existing["id"])
                    )
                    updated += 1
                else:
                    conn.execute(
                        """INSERT INTO companies
                           (name, careers_url, ats_type, ats_handle, sector, hunt_enabled, date_added, source, status)
                           VALUES (?, ?, ?, ?, ?, 1, ?, 'hunter_seed', 'Watchlist')""",
                        (name, url, ats_type, ats_handle, category, today)
                    )
                    inserted += 1
                print(f"[seed] {name:30s} sector={category:25s} ats={ats_type}")

    print(f"[seed] Done. Inserted {inserted}, updated {updated}.")
    return {"inserted": inserted, "updated": updated}


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
)
def list_available_models():
    """List all models available to the current Gemini API key."""
    import os
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    print("[debug] Listing available models...")
    try:
        models = client.models.list()
        available = []
        for m in models:
            # Inspection to find the right attribute
            name = getattr(m, 'name', 'unknown')
            model_id = getattr(m, 'model_id', 'unknown')
            print(f"[debug] Found: {name} / {model_id}")
            available.append(name)
        return available
    except Exception as e:
        print(f"[debug] Failed to list models: {e}")
        return str(e)


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    timeout=300,
)
def probe_model_quota():
    """Try a 1-token prompt against each candidate model. Reports which ones have non-zero quota."""
    import os
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    candidates = [
        "gemini-pro-latest",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash-lite-001",
    ]
    results = {}
    for model in candidates:
        try:
            resp = client.models.generate_content(model=model, contents="ok")
            txt = (resp.text or "").strip()[:30]
            print(f"[probe] OK    {model:35s} -> {txt!r}")
            results[model] = "ok"
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                print(f"[probe] 429   {model:35s} -> rate limited (limit:0 likely)")
                results[model] = "429"
            elif "NOT_FOUND" in msg or "404" in msg:
                print(f"[probe] 404   {model:35s} -> not found")
                results[model] = "404"
            else:
                print(f"[probe] ERR   {model:35s} -> {msg[:80]}")
                results[model] = f"err: {msg[:80]}"
    print(f"[probe] Summary: {results}")
    return results


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
)
def progress_snapshot_cron():
    """Monthly progress snapshot: captures funnel metrics and auto-emits wins.md entry.

    Not scheduled directly (Modal's 5-scheduled-function cap is full — see the
    backup_db() note above) — the monthly run is driven by scheduler() below, gated
    on day-of-month. Kept as a manually-invokable function for on-demand runs and
    verification: `modal run app/main.py::progress_snapshot_cron`.
    """
    from app.observability import configure_logging
    configure_logging()
    init_db()
    from app.crons.progress import snapshot_progress
    result = snapshot_progress()
    print(f"[progress_snapshot_cron] Snapshot captured for {result['snapshot_date']}")
    return result


@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret],
    volumes={"/data": volume},
    timeout=600,
)
@modal.asgi_app()
def web():
    """HTMX web interface."""
    from app.observability import configure_logging
    configure_logging()
    init_db()
    from app.models import capture_repo_baselines, seed_milestones
    capture_repo_baselines()
    seed_milestones()
    from app.routes import create_app
    return create_app(
        batch_research_fn=batch_research_companies,
    )
