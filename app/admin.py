"""One-off admin tools for SearchOps.

    modal run app/admin.py::stamp_applied_at --job-id 140 --applied-date 2026-07-01
    modal run app/admin.py::bulk_close_listings --job-ids 36,101 --no-dry-run

Each tool runs inside the deployed web container, the only process allowed to
write the database (see app/background.py), and prints what it did here.

This is a separate Modal app on purpose. `modal run app/main.py::...` would
start a temporary copy of the whole app, including a second web container with
the database mounted: a second writer. This app has no web container and no
Volume, so running it is always safe. The web app must be deployed first.
"""
import modal

admin = modal.App("recruiting-engine-admin")
image = modal.Image.debian_slim(python_version="3.12")  # only needs the modal client

# Gemini diagnostics call the API directly and never touch the database.
gemini_image = modal.Image.debian_slim(python_version="3.12").pip_install("google-genai")
recruiting_secrets = modal.Secret.from_name("recruiting-secrets")

# Same names as app/main.py; tests/test_single_writer.py checks they match.
job_queue = modal.Queue.from_name("recruiting-engine-jobs", create_if_missing=True)
job_results = modal.Dict.from_name("recruiting-engine-job-results", create_if_missing=True)


def _ids(text: str) -> list[int]:
    """`modal run` can't pass a list, so id lists arrive as "36,101"."""
    return [int(x) for x in text.replace("[", "").replace("]", "").split(",") if x.strip()]


def _relay(name: str, kwargs: dict, timeout: int):
    """Queue job `name` for the web container, wait for it, print its output,
    and return its result."""
    import time
    import uuid

    job_id = uuid.uuid4().hex
    job_queue.put({"id": job_id, "name": name, "kwargs": kwargs, "wait": True})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = job_results.get(job_id)
        if res is not None:
            job_results.pop(job_id)
            if res.get("output"):
                print(res["output"], end="")
            if not res["ok"]:
                raise RuntimeError(f"{name} failed in the web container: {res['error']}")
            return res["result"]
        time.sleep(2)
    raise TimeoutError(
        f"{name} still running after {timeout}s. It keeps going in the web container; "
        "see `modal app logs recruiting-engine`.")



@admin.function(image=image, timeout=420)
def backup_db():
    return _relay("backup", {}, timeout=300)


@admin.function(image=image, timeout=240)
def diagnose_stubs():
    return _relay("diagnose_stubs", {}, timeout=120)


@admin.function(image=image, timeout=720)
def remediate_bucket1():
    return _relay("remediate_bucket1", {}, timeout=600)


@admin.function(image=image, timeout=3720)
def rescore_stale(dry_run: bool = False):
    return _relay("rescore_stale", {"dry_run": dry_run}, timeout=3600)


@admin.function(image=image, timeout=180)
def stamp_applied_at(job_id: int, applied_date: str, dry_run: bool = True):
    return _relay("stamp_applied_at", {"job_id": job_id, "applied_date": applied_date, "dry_run": dry_run}, timeout=60)


@admin.function(image=image, timeout=180)
def correct_erroneous_applied_at(job_id: int, expected_applied_at: str, dry_run: bool = True):
    return _relay("correct_erroneous_applied_at", {"job_id": job_id, "expected_applied_at": expected_applied_at, "dry_run": dry_run}, timeout=60)


@admin.function(image=image, timeout=180)
def stamp_careers_url(company_name: str, careers_url: str, dry_run: bool = True):
    return _relay("stamp_careers_url", {"company_name": company_name, "careers_url": careers_url, "dry_run": dry_run}, timeout=60)


@admin.function(image=image, timeout=180)
def change_pipeline_stage(job_id: int, to_stage: str, decline_reason: str = "", notes: str = "", dry_run: bool = True):
    return _relay("change_pipeline_stage", {"job_id": job_id, "to_stage": to_stage, "decline_reason": decline_reason, "notes": notes, "dry_run": dry_run}, timeout=60)


@admin.function(image=image, timeout=420)
def bulk_close_listings(job_ids: str, decline_reason: str = "Listing removed", dry_run: bool = True):
    return _relay("bulk_close_listings", {"job_ids": _ids(job_ids), "decline_reason": decline_reason, "dry_run": dry_run}, timeout=300)


@admin.function(image=image, timeout=180)
def backfill_decline_outcomes(job_ids: str = "", dry_run: bool = True):
    return _relay("backfill_decline_outcomes", {"job_ids": _ids(job_ids) or None, "dry_run": dry_run}, timeout=60)


@admin.function(image=image, timeout=14520)
def backfill_legacy_research(only_name: str | None = None):
    return _relay("backfill_legacy_research", {"only_name": only_name}, timeout=14400)


@admin.function(image=image, timeout=2820)
def run_discovery_scan_remote():
    return _relay("discovery_scan", {}, timeout=2700)


@admin.function(image=image, timeout=240)
def seed_hunt_targets_remote():
    return _relay("seed_hunt_targets_remote", {}, timeout=120)


@admin.function(image=image, timeout=420)
def progress_snapshot_cron():
    return _relay("progress_snapshot", {}, timeout=300)


@admin.function(image=gemini_image, secrets=[recruiting_secrets], timeout=300)
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


@admin.function(image=gemini_image, secrets=[recruiting_secrets], timeout=300)
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
