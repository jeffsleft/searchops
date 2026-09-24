# evals/ — Validate + Keep (AI_RULES §3)

Checks the scoring engine's AI output for the failure that matters most:
**fabrication**, meaning evidence or metrics the candidate never had.

Runs only on committed example data (the fictional "Alex Rivera" corpus in
`data/Accomplishments_Inventory.example.docx`, `seed_data/example_jds.json`,
`candidate_profile.example.yaml`). Never on the real inventory.

| Stage | Cost | What it checks |
|---|---|---|
| 1. Rules | free | Layer 1 auto-reject: gambling rejects; HealthTech SaaS does **not** (only Traditional Healthcare is blocked) |
| 2. Match | Gemini | Real Layer 2 (`app/scoring/match.py`): valid shape, score in the expected band, every cited accomplishment traceable to the corpus, every number in bullets/hooks present in the corpus or JD, then an LLM judge scoring fabrication (pass ≥ 4/5) |

```bash
python evals/run_evals.py --offline   # stage 1 only; what CI runs on every PR
python evals/run_evals.py             # both stages; needs GEMINI_API_KEY
python evals/run_evals.py --only forgeline_vp_gtm_ops --json
```

Exit code is non-zero on any failure, so it gates a deploy.

**Keep:** `.github/workflows/keep.yml` runs `pip-audit` plus the evals every Monday.
Add the repo secret `EVALS_GEMINI_API_KEY` to include stage 2; without it the
weekly run is offline only. Failures arrive as GitHub's failed-workflow email.

Add a case in `golden_set.json` (`seed_id` from `seed_data/example_jds.json`,
`expect_auto_reject`, and for `"llm": true` a `match_score_range` and a `rubric`).
Checks live in `checks.py`; the judge is `grader.py` (provider-agnostic, from
`~/Projects/project-starter`).
