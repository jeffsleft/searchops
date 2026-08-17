"""One-time backfill: push historical Claude Desktop interview sessions into
SearchOps via the same POST /api/sync/interview-session endpoint the
interview-scorecard skill uses going forward (docs/outcome-charter-interview-sync.md,
Phase 4).

Usage:
    SEARCHOPS_SYNC_TOKEN=... python3 scripts/backfill_interview_sessions.py sessions.json [--live]

Input file: a JSON list of session objects, e.g.:
[
  {
    "company": "Acme Corp",
    "date": "2026-07-14",
    "mode": "live",
    "questions_covered": ["Tell me about your GitLab experience"],
    "self_eval_scores": {"narrative_clarity": 3, "evidence_specificity": 4,
                          "question_handling": 3, "executive_presence": 3,
                          "fit_differentiation": 4, "curiosity_questions": 4},
    "notes": "Weighted read + two things to sharpen, combined.",
    "transcript": "optional full transcript text",
    "session_label": "optional, e.g. 'Panel' or 'Comp call' - required to
                       distinguish two real sessions for the same company on
                       the same day and mode (otherwise the second POST reads
                       as a duplicate of the first and overwrites its scores)"
  },
  ...
]

Without --live, does a dry run: validates the file and prints what would be
sent, without POSTing anything. The endpoint dedupes server-side on
(company, date, mode, session_label), so re-running against the same file is
safe. Two records sharing company/date/mode MUST carry distinct session_label
values or the second will silently overwrite the first's self_eval_scores.
"""
import json
import os
import sys
import urllib.request
import urllib.error

ENDPOINT = "https://jeffsleft--recruiting-engine-web.modal.run/api/sync/interview-session"
REQUIRED_FIELDS = ("company", "date", "mode")
VALID_MODES = ("prep", "live", "debrief")


def _validate(record: dict, index: int) -> list[str]:
    errors = []
    for field in REQUIRED_FIELDS:
        if not record.get(field):
            errors.append(f"record {index}: missing required field '{field}'")
    if record.get("mode") and record["mode"] not in VALID_MODES:
        errors.append(f"record {index}: mode must be one of {VALID_MODES}, got '{record['mode']}'")
    return errors


def _post(record: dict, token: str) -> dict:
    body = json.dumps(record).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": "error", "message": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"}
    except urllib.error.URLError as e:
        return {"status": "error", "message": f"connection failed: {e.reason}"}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_path = sys.argv[1]
    live = "--live" in sys.argv[2:]

    with open(input_path) as f:
        records = json.load(f)

    if not isinstance(records, list):
        print("Input file must be a JSON list of session objects.")
        sys.exit(1)

    all_errors = []
    for i, record in enumerate(records):
        all_errors.extend(_validate(record, i))
    if all_errors:
        print(f"Validation failed ({len(all_errors)} error(s)):")
        for e in all_errors:
            print(f"  - {e}")
        sys.exit(1)

    # Two records sharing (company, date, mode) with no session_label to tell
    # them apart will collide server-side — the second overwrites the first's
    # self_eval_scores instead of creating its own session. Catch it here
    # rather than discovering it after a --live run.
    seen = {}
    collision_errors = []
    for i, record in enumerate(records):
        key = (record["company"], record["date"], record["mode"])
        label = (record.get("session_label") or "").strip()
        if key in seen and not label:
            collision_errors.append(
                f"record {i}: same (company, date, mode) as record {seen[key]} "
                f"({key}) with no session_label — the second POST will overwrite "
                f"the first's self_eval_scores. Add a distinct session_label to both."
            )
        seen[key] = i
    if collision_errors:
        print(f"Collision check failed ({len(collision_errors)} error(s)):")
        for e in collision_errors:
            print(f"  - {e}")
        sys.exit(1)

    print(f"{len(records)} record(s) validated.")

    if not live:
        print("\nDRY RUN — nothing sent. Re-run with --live to actually sync.")
        for i, r in enumerate(records):
            print(f"  [{i}] {r['company']} — {r['date']} ({r['mode']})")
        return

    token = os.environ.get("SEARCHOPS_SYNC_TOKEN")
    if not token:
        print("SEARCHOPS_SYNC_TOKEN env var not set — this is the SearchOps app "
              "password, used as a bearer token. Set it in your shell for this "
              "run only; don't paste it into any file or chat.")
        sys.exit(1)

    results = {"success": 0, "dedup": 0, "error": 0}
    for i, record in enumerate(records):
        resp = _post(record, token)
        label = f"[{i}] {record['company']} — {record['date']} ({record['mode']})"
        if resp.get("status") == "success":
            if resp.get("message") == "Session already exists":
                results["dedup"] += 1
                print(f"{label}: already synced (session_id={resp['session_id']})")
            else:
                results["success"] += 1
                print(f"{label}: synced (session_id={resp['session_id']})")
        else:
            results["error"] += 1
            print(f"{label}: FAILED — {resp.get('message', 'unknown error')}")

    print(f"\n{results['success']} synced, {results['dedup']} already existed, "
          f"{results['error']} failed.")
    if results["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
