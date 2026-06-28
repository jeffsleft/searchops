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
)
def scheduler():
    """
    Single cron entry — runs at midnight, 6am, noon, 6pm UTC.
    - 6am UTC: run automated job discovery scan.
    - Monday 8am PT: also send weekly Slack digest.
    """
    from datetime import datetime, timezone, timedelta

    init_db()

    # Also research a batch of 5 outreach targets if they exist
    from app.models import get_db
    with get_db() as conn:
        rows = conn.execute("SELECT id FROM companies WHERE research_date IS NULL LIMIT 5").fetchall()
    if rows:
        print(f"[scheduler] Triggering background research for {len(rows)} outreach targets.")
        batch_research_companies.spawn([r["id"] for r in rows])

    now_utc = datetime.now(timezone.utc)

    # Run discovery scan daily at 6am UTC; run_discovery_scan() sends the
    # score-aware Slack digest ("N new roles ≥ 8.0 today") when it finds new roles.
    if now_utc.hour == 6:
        from app.discovery.hunter import run_discovery_scan
        run_discovery_scan()

    # Send weekly digest on Monday 8am PT (== Tuesday 4am UTC)
    for utc_offset in [-7, -8]:
        local = now_utc + timedelta(hours=utc_offset)
        if local.weekday() == 0 and local.hour == 8:
            from app.notifications.slack import send_weekly_digest
            send_weekly_digest()
            break

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
    timeout=600,
)
@modal.asgi_app()
def web():
    """HTMX web interface."""
    init_db()
    from app.routes import create_app
    return create_app(
        batch_research_fn=batch_research_companies,
    )
