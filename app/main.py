import logging
import threading
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
    print(f"[main] WARNING: {_INVENTORY} not found — Layer 2 (match) will fall back to the example corpus.")

# W2: the fictional example corpus is committed, but the image adds `data/` files one by
# one — nothing mounts the directory wholesale. Without this, a forker who deploys to Modal
# gets no corpus at all and an empty Application Kit, even though the file is in their repo.
_EXAMPLE_INVENTORY = _ROOT / "data" / "Accomplishments_Inventory.example.docx"
if _EXAMPLE_INVENTORY.exists():
    image = image.add_local_file(
        str(_EXAMPLE_INVENTORY), "/root/data/Accomplishments_Inventory.example.docx"
    )
else:
    print(f"[main] WARNING: {_EXAMPLE_INVENTORY} not found — run scripts/build_example_corpus.py.")

# Resume template for tailored PDF assembler. Optional — route degrades gracefully if absent.
_RESUME = Path(__file__).resolve().parent.parent / "data" / "resume.docx"
if _RESUME.exists():
    image = image.add_local_file(str(_RESUME), "/root/data/resume.docx")
else:
    print(f"[main] WARNING: {_RESUME} not found — /job/{{id}}/resume will return a parse error.")

recruiting_secrets = modal.Secret.from_name("recruiting-secrets")
anthropic_secret = modal.Secret.from_name("anthropic-key")
# Separate secret, not folded into recruiting-secrets (`modal secret create`
# has no additive "add one key" mode) — keeps the Claude-Desktop notes token
# independently revocable from APP_PASSWORD. Only web() needs it; crons never
# serve /api/notes/.
notes_api_secret = modal.Secret.from_name("notes-api-token")

# Single writer. Only web() mounts `volume`, and it runs as exactly one container.
# Every other function reaches the database by putting a job on `job_queue`; the
# web container's consumer thread runs it and posts the result to `job_results`.
# Why: app/background.py. Never add `volumes=` to another function.
#
# Never `modal run` or `modal serve` this file. Either starts a temporary copy of
# the whole app, including a second web() with the Volume mounted, which is a
# second writer that also drains the shared job queue. One-off tools live in
# app/admin.py, a separate app with no web() and no Volume.
job_queue = modal.Queue.from_name("recruiting-engine-jobs", create_if_missing=True)
job_results = modal.Dict.from_name("recruiting-engine-job-results", create_if_missing=True)

_commit_lock = threading.Lock()


def _commit_volume():
    """Persist the Volume. Holds a SQLite write lock for the duration so the
    snapshot never catches a half-written transaction (a writer mid-commit
    leaves the .db and its rollback journal out of step)."""
    import sqlite3
    from app.config import DATABASE_PATH

    with _commit_lock:
        conn = sqlite3.connect(DATABASE_PATH, timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE")  # waits for any in-flight write to finish
            volume.commit()
        finally:
            conn.rollback()
            conn.close()


def _enqueue(name: str) -> None:
    """Queue job `name` for the web container without waiting. app/admin.py
    has the waiting version for one-off tools."""
    job_queue.put({"id": None, "name": name, "kwargs": {}, "wait": False})


def _reply_fn(job_id: str, wait: bool):
    def reply(res: dict):
        if wait:
            try:
                job_results.put(job_id, res)
            except Exception:
                logging.getLogger("app.background").exception("could not post result for %s", job_id)
    return reply


def _consume_jobs(runner):
    """Web-container thread: run each queued job on the BackgroundRunner."""
    import queue
    import time
    from app.background import run_capturing_output
    from app.background_jobs import JOBS

    log = logging.getLogger("app.background")
    while True:
        try:
            msg = job_queue.get(block=True, timeout=60)
        except queue.Empty:
            continue
        except Exception as e:
            # Routine when Modal recycles or replaces this container: the blocking
            # read is cut off mid-wait. One line, not a traceback.
            log.warning("job queue read interrupted (%s); retrying", type(e).__name__)
            time.sleep(5)
            continue
        if not msg:
            continue
        name = msg.get("name")
        reply = _reply_fn(msg.get("id"), msg.get("wait", False))
        fn = JOBS.get(name) or ADMIN_JOBS.get(name)
        if fn is None:
            log.error("unknown job %r", name)
            reply({"ok": False, "error": f"unknown job {name!r}"})
            continue
        fut = runner.submit(name, run_capturing_output, fn, **msg.get("kwargs", {}))
        if fut is None:
            reply({"ok": False, "error": f"{name} is already running"})
            continue

        def _done(f, reply=reply):
            try:
                result, output = f.result()
                reply({"ok": True, "result": result, "output": output})
            except Exception as e:
                reply({"ok": False, "error": repr(e)[:500]})

        fut.add_done_callback(_done)


# Scheduler runs at midnight, 6am, noon, 6pm UTC. It only enqueues: every job
# runs in the web container, the single Volume writer (see the note above).
@app.function(
    image=image,
    schedule=modal.Cron("0 0,6,12,18 * * *"),
    timeout=60,
)
def scheduler():
    """Queue this tick's jobs for the web container.

    - every tick: research up to 5 un-researched outreach targets
    - 00 UTC: prune observability tables
    - 06 UTC: discovery scan (sends its own score-aware Slack digest)
    - Monday 18 UTC (11am PDT / 10am PST): weekly Slack digest
    - Sunday 00 UTC: DB backup (folded in here: Modal caps the workspace at 5 crons)
    - 1st of month 00 UTC: progress snapshot (same cap)
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    jobs = ["research"]
    if now.hour == 0:
        jobs.append("prune_observability")
    if now.hour == 6:
        jobs.append("discovery_scan")
    if now.weekday() == 0 and now.hour == 18:
        jobs.append("weekly_digest")
    if now.weekday() == 6 and now.hour == 0:
        jobs.append("backup")
    if now.day == 1 and now.hour == 0:
        jobs.append("progress_snapshot")
    for name in jobs:
        _enqueue(name)
    print(f"[scheduler] queued: {', '.join(jobs)}")


# On-demand backup: `modal run app/admin.py::backup_db`. Snapshots recruiting.db to
# /data/backups (rotates last 8).


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
# the JD sourced before anything can be scored). Invoke: `modal run app/admin.py::diagnose_stubs`


def _diagnose_stubs_impl():
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
# jd_text. Invoke: `modal run app/admin.py::remediate_bucket1`


def _remediate_bucket1_impl():
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
#   modal run app/admin.py::rescore_stale --dry-run   (list targets, no LLM calls)
#   modal run app/admin.py::rescore_stale             (real run, ~5s pacing per job)
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


def _rescore_stale_impl(dry_run: bool = False):
    import time

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


# One-off admin fix (W1-REDACT, 2026-07-14 plan): Tebra (id 140) has an application
# sent but never got its applied_at stamped, so KR1 and the calibration count
# undercount it. Refuses to overwrite a row that already has applied_at set — this
# is a single deliberate backfill, not a general-purpose field editor.
# Invoke: `modal run app/admin.py::<name>` (runs in the deployed web container),
# off the 0/6/12/18 UTC cron ticks: dry_run first, then dry_run=False to commit.


def _stamp_applied_at_impl(job_id: int, applied_date: str, dry_run: bool = True):
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

    print(f"[stamp_applied_at] id={job_id} committed.")


# One-off correction (2026-07-16, W1-T session): a job_detail.html placeholder string
# in the notes textarea ("Applied 2026-05-23...") was misread as real saved data during
# W1-T triage, leading to a bad stamp_applied_at + they_declined transition on job 23
# (Decagon) and an auto-logged application_outcomes row that never should have existed.
# Undoes exactly that: clears applied_at only if it matches the erroneous value, deletes
# the outcome row only if its notes carry this session's marker text (so this can't
# accidentally touch a legitimate row). Pipeline stage was already reverted via the UI
# (record_stage_change) before this runs.


def _correct_erroneous_applied_at_impl(job_id: int, expected_applied_at: str, dry_run: bool = True):
    from app.models import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, company, applied_at, pipeline_stage FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            print(f"[correct_erroneous_applied_at] no job with id={job_id} — nothing done")
            return
        if row["applied_at"] != expected_applied_at:
            print(
                f"[correct_erroneous_applied_at] id={job_id} '{row['company']}' "
                f"applied_at={row['applied_at']!r} does not match expected "
                f"{expected_applied_at!r} — refusing to touch"
            )
            return

        outcome_rows = conn.execute(
            "SELECT id, outcome, notes FROM application_outcomes "
            "WHERE job_id = ? AND notes LIKE '%W1-T triage%'",
            (job_id,),
        ).fetchall()

        print(
            f"[correct_erroneous_applied_at] id={job_id} '{row['company']}' "
            f"pipeline_stage={row['pipeline_stage']} -> clear applied_at "
            f"(was {expected_applied_at}); delete {len(outcome_rows)} outcome row(s) "
            f"{[o['id'] for o in outcome_rows]} (dry_run={dry_run})"
        )
        if dry_run:
            return

        conn.execute("UPDATE jobs SET applied_at = NULL WHERE id = ?", (job_id,))
        conn.execute(
            "DELETE FROM application_outcomes WHERE job_id = ? AND notes LIKE '%W1-T triage%'",
            (job_id,),
        )

    print(f"[correct_erroneous_applied_at] id={job_id} committed.")


# Careers-URL backfill (2026-07-27, per Todoist "Add careers URLs to Tier A
# companies"): 47 of 60 Tier A companies had no careers_url, blocking the
# discovery scan's ATS detection for them. Each URL must be verified against
# live content, not just a 200 status -- jobs.ashbyhq.com/{anything} returns
# HTTP 200 for every slug (a client-side SPA shell), and generic company-name
# slugs can collide with an unrelated company on the same ATS (jobs.lever.co/
# alloy resolves to "Alloy.ai", a supply-chain analytics company, not the
# fintech identity platform actually on this list). See
# memory/lessons_learned.md for the full verification method.


def _stamp_careers_url_impl(company_name: str, careers_url: str, dry_run: bool = True):
    from app.models import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, careers_url FROM companies WHERE name = ? AND tier_a = 1",
            (company_name,),
        ).fetchone()
        if not row:
            print(f"[stamp_careers_url] no Tier A company named {company_name!r} — nothing done")
            return
        if row["careers_url"]:
            print(
                f"[stamp_careers_url] '{company_name}' already has "
                f"careers_url={row['careers_url']!r} — refusing to overwrite"
            )
            return

        print(f"[stamp_careers_url] '{company_name}' -> careers_url={careers_url} (dry_run={dry_run})")
        if dry_run:
            return

        conn.execute(
            "UPDATE companies SET careers_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (careers_url, row["id"]),
        )

    print(f"[stamp_careers_url] '{company_name}' committed.")


# W1-T session (2026-07-16): the browser-driven /job/{id}/stage route lost a write
# (Diligent, job 88 -> job_listing_closed — POST returned 200, live app reflected the
# change, but a fresh `modal volume get` pull kept showing the pre-change state, no new
# pipeline_history row). Same failure class as the documented SQLite-on-Volume
# last-writer-wins race (Session 47/51) — the route's write path has no explicit
# volume.commit(). This wraps the exact same sanctioned call the route uses
# (advance_stage -> record_stage_change) with an explicit commit, for reliability during
# this session's triage. Not a permanent fix — the underlying route gap is still open.


def _change_pipeline_stage_impl(job_id: int, to_stage: str, decline_reason: str = "", notes: str = "", dry_run: bool = True):
    from app.pipeline.tracker import advance_stage, STAGES
    from app.models import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, company, pipeline_stage FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row:
        print(f"[change_pipeline_stage] no job with id={job_id} — nothing done")
        return
    if to_stage not in STAGES:
        print(f"[change_pipeline_stage] unknown stage {to_stage!r} — nothing done")
        return

    print(
        f"[change_pipeline_stage] id={job_id} '{row['company']}' "
        f"{row['pipeline_stage']} -> {to_stage} (reason={decline_reason!r}, dry_run={dry_run})"
    )
    if dry_run:
        return

    result = advance_stage(job_id, to_stage, notes=notes, decline_reason=decline_reason)
    if not result["ok"]:
        print(f"[change_pipeline_stage] id={job_id} FAILED: {result['error']}")
        return

    print(f"[change_pipeline_stage] id={job_id} committed: {result}")


# Batched sibling of change_pipeline_stage for stale-listing cleanup (2026-09-20): one
# container, one volume.commit(), so a bulk close can't hit the rapid-container-churn
# last-writer-wins clobber (Session 47/51). Only touches jobs still in a pre-application
# stage, so a job that moved on since the liveness check is skipped, never auto-closed.
# Invoke: `modal run app/admin.py::bulk_close_listings`, dry_run first. Verify with a
# fresh `modal volume get`, not another container.


def _bulk_close_listings_impl(job_ids: list, decline_reason: str = "Listing removed", dry_run: bool = True):
    from app.pipeline.tracker import advance_stage
    from app.models import get_db

    closable_from = ("discovered", "identified")
    with get_db() as conn:
        rows = {
            r["id"]: r for r in conn.execute(
                f"SELECT id, company, job_title, pipeline_stage FROM jobs WHERE id IN ({','.join('?' * len(job_ids))})",
                job_ids,
            ).fetchall()
        }

    actions, closed = [], 0
    for job_id in job_ids:
        row = rows.get(job_id)
        if not row:
            actions.append(f"id={job_id} SKIP not found")
        elif row["pipeline_stage"] not in closable_from:
            actions.append(f"id={job_id} '{row['company']}' SKIP stage={row['pipeline_stage']} (not pre-application)")
        elif dry_run:
            actions.append(f"id={job_id} '{row['company']}' | {row['job_title']} WOULD CLOSE from {row['pipeline_stage']}")
        else:
            result = advance_stage(job_id, "job_listing_closed", notes="bulk stale-listing sweep", decline_reason=decline_reason)
            if result["ok"]:
                closed += 1
                actions.append(f"id={job_id} '{row['company']}' CLOSED from {row['pipeline_stage']}")
            else:
                actions.append(f"id={job_id} '{row['company']}' FAILED: {result['error']}")

    for a in actions:
        print(f"[bulk_close_listings] {a}")
    print(f"[bulk_close_listings] Done: {closed} closed of {len(job_ids)} (dry_run={dry_run}).")
    return {"dry_run": dry_run, "closed": closed, "actions": actions}


# One-off backfill (W1-B, 2026-07-14 plan): 5 declined applications sent their
# outcome-logging before record_stage_change auto-logged (or via a path that bypassed
# it), so they never produced an application_outcomes row and KR2 undercounts. This
# replays the auto-logging rules exactly — outcome only where applied_at IS NOT NULL,
# mapped from the job's CURRENT stage, idempotent (skip if a row already exists).
# Invoke: `modal run app/admin.py::<name>` (runs in the deployed web container), AFTER
# the PR is merged + deployed, off the 0/6/12/18 UTC cron ticks: dry_run first, then
# dry_run=False. Default targets are the 5 unlogged declines (Notion 32, Semrush 39,
# NerdWallet 60, Airwallex 102, Asana 103); pass job_ids to override.


def _backfill_decline_outcomes_impl(job_ids: list = None, dry_run: bool = True):
    from app.models import get_db
    from app.services.pipeline_service import STAGE_TO_OUTCOME
    from app.services.calibration_service import record_outcome

    targets = job_ids if job_ids else [32, 39, 60, 102, 103]
    actions = []  # returned to the caller so the plan is reviewable off a dry run
    logged = 0
    with get_db() as conn:
        for job_id in targets:
            row = conn.execute(
                "SELECT id, company, pipeline_stage, applied_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                actions.append(f"id={job_id} SKIP not found")
                continue
            if not row["applied_at"]:
                actions.append(f"id={job_id} '{row['company']}' SKIP no applied_at (not an application)")
                continue
            outcome = STAGE_TO_OUTCOME.get(row["pipeline_stage"])
            if not outcome:
                actions.append(f"id={job_id} '{row['company']}' SKIP stage={row['pipeline_stage']} maps to no outcome")
                continue
            existing = conn.execute(
                "SELECT 1 FROM application_outcomes WHERE job_id = ? AND outcome = ? LIMIT 1",
                (job_id, outcome),
            ).fetchone()
            if existing:
                actions.append(f"id={job_id} '{row['company']}' SKIP already has '{outcome}' (idempotent)")
                continue
            verb = "WOULD LOG" if dry_run else "LOGGED"
            actions.append(f"id={job_id} '{row['company']}' stage={row['pipeline_stage']} -> {verb} '{outcome}'")
            if not dry_run:
                record_outcome(job_id, outcome, notes="backfill: W1-B unlogged decline", conn=conn)
                logged += 1

    summary = {"dry_run": dry_run, "logged": logged, "actions": actions}
    for a in actions:
        print(f"[backfill_outcomes] {a}")
    print(f"[backfill_outcomes] Done: {logged} logged (dry_run={dry_run}).")
    return summary


def _backfill_legacy_research_impl(only_name: str | None = None):
    """Re-research companies whose research_json predates the fit/need-justification feature.

    Targets rows that have been researched (research_date IS NOT NULL) but whose JSON
    lacks the fit_justification field. Optionally restrict to a single company via
    `only_name` for spot-checking.

    Runs the companies one after another in the web container (single writer).
    """
    import json
    from app.models import get_db

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

    from app.background_jobs import research_companies
    print(f"[backfill] re-researching {len(legacy_ids)} companies")
    done = research_companies(legacy_ids)
    print(f"[backfill] done: {done}/{len(legacy_ids)} successful")
    return {"queued": len(legacy_ids), "done": done}


# Manual discovery scan, e.g. right after seeding hunt targets:
# `modal run app/admin.py::run_discovery_scan_remote`. Runs in the web container;
# a full ~64-company run takes ~2000s.


def _seed_hunt_targets_remote_impl():
    """Seed companies table from app/discovery/hunt_targets.yaml against the Volume-backed DB.

    Idempotent: existing companies (matched by name) are updated with hunt_enabled=1
    and refreshed careers_url/ats fields. New companies are inserted with source='hunter_seed'.
    """
    import yaml
    from datetime import date
    from pathlib import Path
    from app.models import get_db
    from app.discovery.ats_clients import detect_ats

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


# One-off (2026-09-24, Jeff approved): bulk triage from the Discovered inbox was
# recorded as i_declined before the 'dismissed' stage existed, inflating the
# considered-decline count. Moves only jobs never applied to, declined straight out
# of discovered/identified, whose latest decline note is one of the bulk-triage
# reasons below. Real declines (in-office, culture, comp, anything researched)
# stay put. Goes through record_stage_change so each move has a history row.
# Invoke: `modal run app/admin.py::reclassify_triage_declines` (dry run), then
# `--no-dry-run`.
_TRIAGE_NOTE_PREFIXES = (
    "60+ days old", "45+ days old", "Non-US region", "Below target seniority",
    "Could not score automatically", "JD fetch hung", "Declined per W1-T backlog triage",
)


def _reclassify_triage_declines_impl(dry_run: bool = True):
    from app.models import get_db
    from app.services.pipeline_service import record_stage_change

    with get_db() as conn:
        rows = conn.execute(
            """SELECT j.id, j.company, h.notes FROM jobs j
               JOIN pipeline_history h ON h.id = (
                   SELECT MAX(h2.id) FROM pipeline_history h2
                   WHERE h2.job_id = j.id AND h2.to_stage = 'i_declined')
               WHERE j.pipeline_stage = 'i_declined' AND j.applied_at IS NULL
                 AND h.from_stage IN ('discovered', 'identified')"""
        ).fetchall()
        targets = [r for r in rows if (r["notes"] or "").startswith(_TRIAGE_NOTE_PREFIXES)]
        print(f"[reclassify] {len(targets)} of {len(rows)} inbox declines match a triage note (dry_run={dry_run})")
        for r in targets:
            print(f"[reclassify] id={r['id']} '{r['company']}' — {(r['notes'] or '')[:50]!r}")
            if not dry_run:
                record_stage_change(conn, r["id"], "dismissed",
                                    note="Reclassified: bulk inbox triage, not a considered decline",
                                    changed_by="reclassify")
    return {"dry_run": dry_run, "matched": len(targets), "moved": 0 if dry_run else len(targets)}


# One-off (2026-09-25): until the scan kept the job board's own description,
# discovered roles on JS-rendered boards were saved with no JD and never scored.
# Re-reads each affected company's feed once, fills jd_text by matching the job
# URL, and scores through the canonical path (same per-run LLM cap as a scan).
# Invoke: `modal run app/admin.py::backfill_missing_jds` (dry run), then `--no-dry-run`.
def _backfill_missing_jds_impl(dry_run: bool = True):
    from collections import defaultdict
    from app.config import DISCOVERY_FULL_SCORE_CAP, load_profile
    from app.discovery.ats_clients import fetch_ashby_description, fetch_jobs_for_company, html_to_text
    import time
    from app.discovery.hunter import _LLM_CALL_PACING_SECONDS, _MIN_JD_FOR_SCORE, _auto_score_discovery
    from app.models import get_db

    with get_db() as conn:
        rows = conn.execute(
            # Every unscored discovered role, not only the ones missing a JD: a role
            # whose JD was saved but whose scoring hit a rate limit gets retried too.
            """SELECT j.id, j.url, j.company, j.jd_text, c.ats_type, c.ats_handle, c.careers_url
               FROM jobs j JOIN companies c ON c.id = j.company_id
               WHERE j.pipeline_stage = 'discovered' AND j.final_score IS NULL
                 AND j.auto_rejected = 0"""
        ).fetchall()
    by_company = defaultdict(list)
    for r in rows:
        by_company[(r["company"], r["ats_type"], r["ats_handle"], r["careers_url"])].append(r)

    profile, budget, results = load_profile(), {"remaining": DISCOVERY_FULL_SCORE_CAP}, []
    for (company, ats_type, handle, careers_url), jobs in by_company.items():
        need = [r for r in jobs if len(r["jd_text"] or "") < _MIN_JD_FOR_SCORE]
        if not need:
            feed = {}
        elif ats_type == "ashby":
            # Ashby fetches details one posting at a time; read only the ones needed
            # (OpenAI's board has hundreds), paced like fetch_ashby_jobs to avoid 429s.
            feed = {}
            for r in need:
                time.sleep(0.3)
                feed[r["url"]] = html_to_text(fetch_ashby_description(handle, r["url"].rstrip("/").rsplit("/", 1)[-1]))
        else:
            feed = {j["url"]: html_to_text(j.get("description", ""))
                    for j in fetch_jobs_for_company(ats_type, handle, careers_url)}
        for r in jobs:
            text = r["jd_text"] if len(r["jd_text"] or "") >= _MIN_JD_FOR_SCORE else feed.get(r["url"], "")
            status = "no_feed_text" if len(text) < _MIN_JD_FOR_SCORE else "would_score"
            if status == "would_score" and not dry_run:
                with get_db() as conn:
                    conn.execute("UPDATE jobs SET jd_text = ? WHERE id = ?", (text, r["id"]))
                status = _auto_score_discovery(r["id"], r["url"], profile, budget, feed_text=text)
                time.sleep(_LLM_CALL_PACING_SECONDS)  # free-tier pacing, same as the scan
            results.append(f"id={r['id']} {company}: {status} ({len(text)} chars)")
            print(f"[backfill_jd] id={r['id']} {company}: {status} ({len(text)} chars)")
    print(f"[backfill_jd] {len(rows)} role(s) missing a JD (dry_run={dry_run})")
    return {"dry_run": dry_run, "results": results}


# Admin one-offs above, run by the web container's job consumer.
ADMIN_JOBS = {
    name: globals()[f"_{name}_impl"]
    for name in (
        "diagnose_stubs", "remediate_bucket1", "rescore_stale", "stamp_applied_at",
        "correct_erroneous_applied_at", "stamp_careers_url", "change_pipeline_stage",
        "bulk_close_listings", "backfill_decline_outcomes", "backfill_legacy_research",
        "seed_hunt_targets_remote", "reclassify_triage_declines", "backfill_missing_jds",
    )
}


# The only function that mounts the Volume. Exactly one container, always on:
# a second container would be a second writer (last-writer-wins clobber), and
# always-on removes the cold start. @modal.concurrent lets that one container
# serve overlapping requests; app/background.py keeps each off the event loop.
@app.function(
    image=image,
    secrets=[recruiting_secrets, anthropic_secret, notes_api_secret],
    volumes={"/data": volume},
    timeout=600,
    min_containers=1,
    max_containers=1,
)
@modal.concurrent(max_inputs=32)
@modal.asgi_app()
def web():
    """HTMX web interface."""
    from app.background import BackgroundRunner
    from app.observability import configure_logging
    configure_logging()
    init_db()
    from app.models import capture_repo_baselines, seed_milestones
    capture_repo_baselines()
    seed_milestones()
    from app.routes import create_app
    runner = BackgroundRunner(commit_fn=_commit_volume, max_workers=4)
    threading.Thread(target=_consume_jobs, args=(runner,), daemon=True, name="job-consumer").start()
    return create_app(commit_fn=_commit_volume, background=runner)
