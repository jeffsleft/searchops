"""SearchOps evals: the Validate + Keep rung (AI_RULES §3).

Stage 1 (free, no API): Layer 1 auto-reject rules on the example candidate.
Stage 2 (Gemini): the real Layer 2 match (app/scoring/match.py) on example JDs
against the fictional "Alex Rivera" corpus. Deterministic fabrication checks
(evals/checks.py), then an LLM judge (evals/grader.py) that scores fabrication.

Uses only committed example data, never Jeff's real inventory, so it runs the
same in the public repo and in CI. Exits non-zero on any failure.

    python evals/run_evals.py             # both stages (needs GEMINI_API_KEY)
    python evals/run_evals.py --offline   # stage 1 only
    python evals/run_evals.py --only forgeline_vp_gtm_ops
    python evals/run_evals.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
# The engine reads no-go keywords from SQLite; give it a throwaway database.
os.environ["DATABASE_PATH"] = tempfile.mkstemp(suffix=".db")[1]

import yaml  # noqa: E402

JUDGE_MODEL = os.getenv("EVALS_JUDGE_MODEL", "gemini-flash-lite-latest")


def _load():
    import app.config as config
    import app.models as models
    config.DATABASE_PATH = models.DATABASE_PATH = os.environ["DATABASE_PATH"]
    models.init_db()
    cases = json.loads((HERE / "golden_set.json").read_text())
    jds = {j["id"]: j for j in json.loads((ROOT / "seed_data" / "example_jds.json").read_text())}
    profile = yaml.safe_load((ROOT / "candidate_profile.example.yaml").read_text())
    return cases, jds, profile


def stage_rules(case: dict, jd: dict, profile: dict) -> dict:
    from app.scoring.engine import check_auto_reject
    rejected, reason = check_auto_reject(jd["jd_text"], profile)
    ok = rejected == case["expect_auto_reject"]
    return {"name": "auto_reject", "passed": ok,
            "detail": f"rejected={rejected} ({reason}) expected={case['expect_auto_reject']}"}


def stage_match(case: dict, jd: dict, profile: dict) -> tuple[list[dict], dict | None]:
    from checks import CHECKS
    from grader import grade
    from app.scoring import match
    from app.scoring.corpus import EXAMPLE_INVENTORY_PATH, load_corpus, render_corpus_for_prompt
    from app.scoring.research import _candidate_summary

    corpus = load_corpus(EXAMPLE_INVENTORY_PATH)
    match.load_corpus = lambda *a, **k: corpus  # never Jeff's real inventory
    result = match.score_match(jd["jd_text"], _candidate_summary(profile))

    failed_call = [m for m in result.get("mismatches") or []
                   if m.get("jd_requirement") in ("(layer 2 call failed)", "(corpus not available)")]
    if failed_call:
        return [{"name": "generation", "passed": False, "detail": failed_call[0]["gap"][:300]}], None

    ctx = {"case": case, "corpus_text": render_corpus_for_prompt(corpus), "jd_text": jd["jd_text"]}
    results = []
    # A malformed model reply or a judge error fails this case; it never stops the run.
    for check in CHECKS:
        try:
            ok, detail = check(result, ctx)
        except Exception as e:
            ok, detail = False, f"check crashed on this output: {type(e).__name__}: {e}"
        results.append({"name": check.__name__, "passed": bool(ok), "detail": detail})
    try:
        judged = grade(output=json.dumps(result, indent=1), rubric=case["rubric"],
                       context=f"CORPUS:\n{ctx['corpus_text']}\n\nJOB DESCRIPTION:\n{jd['jd_text']}",
                       provider="gemini", model=JUDGE_MODEL)
    except Exception as e:
        judged = {"score": 0, "passed": False, "reasons": f"judge failed: {type(e).__name__}: {str(e)[:200]}"}
    return results, judged


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="stage 1 only, no API calls")
    ap.add_argument("--only")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cases, jds, profile = _load()
    report = []
    for case in cases:
        if args.only and case["id"] != args.only:
            continue
        jd = jds[case["seed_id"]]
        checks = [stage_rules(case, jd, profile)]
        judged = None
        if case.get("llm") and not args.offline:
            more, judged = stage_match(case, jd, profile)
            checks += more
        passed = all(c["passed"] for c in checks) and (judged is None or judged["passed"])
        report.append({"id": case["id"], "passed": passed, "checks": checks, "grade": judged})

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for r in report:
            print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']}")
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"      x {c['name']}: {c['detail']}")
            if r["grade"]:
                print(f"      judge {r['grade']['score']}/5: {r['grade']['reasons']}")
        n = sum(r["passed"] for r in report)
        print(f"\n{n}/{len(report)} passed" + ("  (offline: stage 1 only)" if args.offline else ""))
    sys.exit(0 if all(r["passed"] for r in report) else 1)


if __name__ == "__main__":
    main()
