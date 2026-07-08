# SearchOps — Progress Instrumentation & Self-Reporting Layer (v2)

> Supersedes `progress-instrumentation-spec.md`. Changes from v1 are marked **[REDLINE]**
> with the reason. Hand to `gemini-builder` (per AI_RULES delegation) or run in a fresh
> Claude Code session. Written to be executed as-is by an AI builder.
>
> Author intent (unchanged): capture measurable outcomes as they occur so career-progress
> metrics are derived from real rows — never reconstructed by hand — and feed them into
> `/wins-rollup` automatically.

## Why v2 exists (read this first)

A verification pass against the live codebase found four problems in v1. They are fixed below.

1. **Two of v1's four seeded baselines were already wrong.** v1 seeded `db_tables = "25"`
   (actual: **26** `CREATE TABLE` statements in `app/models.py`) and `test_count = "63"`
   (actual: **54** `def test_` functions under `tests/`). A spec whose thesis is "derive
   from real rows, never hand-enter" must not hand-enter constants that rot. **[REDLINE]**
   these are now *computed*, not seeded.
2. **`mean_brier` is not computable from the named column.** `jobs.interview_probability`
   is categorical TEXT (`"High" | "Medium" | "Low" | "Unknown"` — see
   `app/pipeline/calibration.py:70`), not a 0–1 probability. A Brier score needs a numeric
   probability; producing one requires inventing a High/Med/Low→number mapping, which
   biases the metric and violates the anti-fabrication rule. **[REDLINE]** Brier removed;
   honest bucketed hit-rate retained.
3. **Calibration already exists — twice.** `app/pipeline/calibration.py`
   (`compute_calibration`, buckets by probability) and `app/services/calibration_service.py`
   (`get_calibration_summary`, `record_outcome`, `get_job_outcomes`). v1 told the builder to
   "add" a third. **[REDLINE]** extend the existing service; do not add a parallel one.
4. **"One write path" is a refactor, not an add.** `pipeline_history` is already written
   from two callers that disagree on columns — `app/pipeline/tracker.py:94`
   (`from_stage, to_stage, notes`) and `app/services/job_actions.py:119`
   (`to_stage, changed_by`, no `from_stage`) — and `app/services/scoring_service.py:85`
   moves `identified → discovered` with **no** history row. **[REDLINE]** scope corrected to
   "consolidate existing callers," with a test that guarantees no path is missed.

## Context
You are in the `recruiting-engine` repo (SearchOps): Modal + Starlette + raw sqlite3
(no ORM) + HTMX/Jinja2, on a Modal Volume. Read `CLAUDE.md` and `~/Projects/AI_RULES.md`
(§3 Modal, §6 SQLite, §9 Python) first.

**Verified facts to build against (confirmed 2026-07-04 — re-confirm with `.schema` before writing):**
- Tables that exist: `jobs`, `pipeline_history`, `application_outcomes`, `score_history`,
  `interview_sessions`, `task_log`, plus 20 others. `application_outcomes` columns are
  `(id, job_id, outcome, notes, recorded_at)`; `outcome` ∈ {`applied`, `phone_screen`,
  `interview`, `offer`, `rejected_them`, `rejected_me`, `ghosted`}.
- Schema + migrations live in `app/models.py` (`SCHEMA` string + `_run_migration()` /
  `init_db()` at the bottom). New tables go in `SCHEMA`; new columns go through
  `_run_migration("ALTER TABLE ... ADD COLUMN ...")`.
- Existing Modal cron style: `modal.Cron("0 0,6,12,18 * * *")` in `app/main.py`. Monthly-1st
  09:00 UTC = `modal.Cron("0 9 1 * *")`.
- Existing calibration: `app/services/calibration_service.py` and `app/pipeline/calibration.py`.
- Existing per-event logger: `log_task_event()` in `app/models.py:469` → `task_log` table.

## Objective (unchanged)
Add a progress-tracking layer that CAPTURES measurable outcomes as they occur and
SELF-REPORTS them monthly. Every emitted metric is either backed by real rows or is
explicitly `"N/A (no data yet)"` / `0` / `NULL`. No fabricated numbers, ever.

## Scope — build exactly these four pieces plus the board view (§5). Do not expand further.

### 1. Backfill baselines (one-time, idempotent) — **[REDLINE: compute, don't seed]**
- New table
  `progress_baselines(id, metric_key TEXT UNIQUE, label TEXT, value TEXT, captured_at TEXT, source TEXT)`.
- Seed (INSERT OR IGNORE) **only the genuinely static, externally-proven deltas**:
  - `index_speedup` = `"19–232x (bench_indexes.py, 20k/5k)"`, source = commit/file proof.
  - `sheets_removal` = `"net -1056 LOC / 3 deps (d5aeea7)"`, source = the commit.
- **Do NOT seed `test_count` or `db_tables` as literals.** Instead, a helper
  `capture_repo_baselines()` computes them at run time and upserts (INSERT OR REPLACE):
  - `db_tables` = `SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'`.
  - `test_count` = number of collected tests, via `subprocess` `pytest --co -q` parsed for
    the final count, or a fallback `grep -rc "def test_" tests/`. Store the method in `source`.
  - Set `captured_at` each run so the value is self-dating and auditable.
- New table `milestones(id, name TEXT, completed_on TEXT, evidence TEXT)`; seed the
  Track A/B work-package + public-launch + case-study milestones from `docs/wins.md`.
  Idempotent on `name`.

### 2. Funnel + calibration instrumentation — **[REDLINE: consolidate, don't duplicate]**
- **Single write path.** Create ONE helper (e.g. `record_stage_change(conn, job_id, to_stage, note=None, changed_by="user")`)
  in `app/services/pipeline_service.py` that (a) UPDATEs `jobs.pipeline_stage` and (b) writes
  exactly one `pipeline_history` row with `from_stage` read from the job's current stage
  before the update. Then **refactor these existing callers to use it**:
  - `app/pipeline/tracker.py:84` (currently writes history at :94)
  - `app/services/job_actions.py:115` (currently writes history at :119, missing `from_stage`)
  - `app/services/scoring_service.py:85` (currently writes NO history row — this is the leak)
  Grep for every `UPDATE jobs SET pipeline_stage` and route each through the helper. Leave a
  code comment on the helper: "the only sanctioned way to change pipeline_stage."
- On terminal transitions (`applied` / `phone_screen` / `interview` / `offer` /
  `rejected_them` / `rejected_me` / `ghosted`), the helper also calls the existing
  `calibration_service.record_outcome(job_id, outcome)` — do not re-implement outcome writes.
- **Extend `calibration_service.get_calibration_summary()`** (do not create a new function) to
  additionally return: `n_scored`, `n_with_outcome`, `hit_rate_by_bucket` (High/Medium/Low →
  interview-or-better rate), and `insufficient_data: true` with explicit nulls when
  `n_with_outcome < CALIBRATION_MIN_SAMPLE` (config, default 5).
  - **[REDLINE]** No `mean_brier`. `hit_rate` is reported *per probability bucket*, not as a
    single scalar, and always accompanied by the bucket's `n`. A bucket with `n < 3` reports
    its rate as `null` regardless of the global threshold — small buckets are noise.
  - Reconcile with `app/pipeline/calibration.py`: if its `compute_calibration()` already
    yields the buckets, have the service call it rather than re-querying. One source of truth.

### 3. Monthly snapshot cron (the delta engine)
- New table
  `progress_snapshots(id, snapshot_date TEXT UNIQUE, jobs_scored INT, companies_covered INT,
  apps_sent INT, interviews INT, offers INT, calibration_hit_rate_json TEXT NULL,
  median_days_to_first_interview REAL NULL, raw_json TEXT)`.
  - **[REDLINE]** `calibration_hit_rate` is stored as JSON (per-bucket), not a single REAL,
    to match §2. `median_days_to_first_interview` added — see §5 rationale (velocity is the
    one metric that is meaningful at low n).
- A Modal cron (`modal.Cron("0 9 1 * *")`) computes each field from live tables and writes one
  row. `raw_json` stores the full computed blob for auditability. Idempotent on `snapshot_date`
  (INSERT OR REPLACE).
- Every numeric field derives from a COUNT/AVG/percentile over real rows. Empty source → `0`
  or `NULL`, never invented. `apps_sent` = count of jobs with an `applied` outcome; `interviews`
  = jobs reaching `interview`; `offers` = jobs reaching `offer`; `companies_covered` = distinct
  company_id on scored jobs.

### 4. Auto-emit a graded wins.md entry (closes the loop)
- After writing a snapshot, compute the delta vs. the previous snapshot row and append ONE
  entry to `docs/wins.md` using this template:

  ```
  ## SearchOps progress snapshot — {snapshot_date}
  - **Objective:** demonstrate the tool drives a real job-search funnel
  - **Baseline:** {prior snapshot values, or "first snapshot — no baseline"}
  - **Result:** {current values + deltas}
  - **Conversion:** applied→screen {x%}, screen→interview {y%}, interview→offer {z%} (n=…)
  - **Proof:** progress_snapshots row {snapshot_date}; /settings/calibration
  ```
  - **[REDLINE]** Added the Conversion line — the funnel *rates*, each with its `n`, are the
    story; the raw counts are not. If a stage has `n < 5`, print `"n=… (directional)"` instead
    of a percentage.
- First snapshot writes `"first snapshot — baseline established"`, never an invented delta.
  Append only; never rewrite existing `wins.md` content above the new entry.

### 5. Board-facing metrics view — **[REDLINE: new in v2]**
Add a read-only service `board_metrics()` (in a new `app/services/progress_service.py`) and a
route `GET /settings/progress` rendering `templates/progress.html`. This is the artifact Jeff
can screenshot for a hiring panel — it reports SearchOps the way a RevOps leader reports a
funnel to a board. It reads only from `progress_snapshots` + live tables; it computes nothing
new that §2–3 don't already persist.

It surfaces, each with an explicit `n` and a confidence tag (`solid` n≥30, `directional`
5≤n<30, `insufficient` n<5):
- **Funnel conversion trend** — applied→screen→interview→offer as rates over the snapshot
  series (a sparkline, not a single point).
- **Velocity** — median days-in-stage and median days-to-first-interview (meaningful at low n).
- **Targeting quality** — interviews per 10 applications; and does score ≥8 convert better than
  score 5–7? (the cohort question that proves the *engine* works).
- **Model calibration** — the per-bucket hit-rate from §2, shown only when `not insufficient_data`,
  always with its `n` and confidence tag.
- **Leading indicators** — apps-in-flight and time-to-first-screen, called out as leading vs. the
  lagging offer count.

Every tile with `n < 5` renders "Insufficient data — n=X" rather than a number. This is the
whole point: the board view is *credible* precisely because it refuses to show noise.

## Customization (config-driven, not hardcoded)
Expose in `app/config.py` (env-backed): `CALIBRATION_MIN_SAMPLE` (default 5),
`PROGRESS_CRON_SCHEDULE` (default `"0 9 1 * *"`), `WINS_AUTOEMIT_ENABLED` (default true),
and `BOARD_CONFIDENCE_SOLID_N` (default 30). No literals in query code.

## Constraints & guardrails (unchanged from v1, still binding)
- Raw sqlite3, parameterized queries only. No ORM. Match existing repo patterns.
- All new tables via `SCHEMA` in `app/models.py`; new columns via guarded `_run_migration` ALTER.
- Additive only: do not alter existing table schemas or delete columns.
- ANTI-FABRICATION (non-negotiable): no computed metric surfaced or written unless backed by
  real rows. Empty source → `0` / `NULL` / `"N/A"`, explicitly. This now *also* means: no
  scalar metric on a sample too small to support it (see the `n` gates above).
- Do not deploy. Stop at: schema + services + cron + board view wired + tests green, and report
  a diff summary. Deployment is human-gated (AI_RULES §3).

## Acceptance criteria — **[REDLINE: tightened]**
1. Fresh `init_db()` creates all new tables idempotently (run twice, no error).
2. `capture_repo_baselines()` writes `db_tables` and `test_count` as *computed* values that
   match `sqlite_master` and `pytest --co` respectively — prove they are not the stale
   literals 25/63.
3. Every code path that changes `pipeline_stage` routes through `record_stage_change`; a test
   greps the codebase and asserts no raw `UPDATE jobs SET pipeline_stage` remains outside the
   helper. A stage change on a test job writes exactly one `pipeline_history` row (with a
   correct `from_stage`) and, on a terminal stage, the matching `application_outcomes` row.
4. `get_calibration_summary()` returns `insufficient_data: true` on <5 outcomes and per-bucket
   rates (each with `n`, buckets with n<3 → null) on ≥5. No `mean_brier` key exists anywhere.
5. Running the snapshot job twice on the same date yields one row, not two.
6. First `wins.md` auto-entry says "baseline established"; a simulated second month shows real
   deltas + a Conversion line with per-stage `n`. Content above the new entry is untouched.
7. `GET /settings/progress` renders; every tile with n<5 shows "Insufficient data", and no tile
   shows a percentage without an accompanying `n`.
8. Unit tests cover: the calibration thin-data branch, the small-bucket-null branch, snapshot
   idempotency, and the "no stray pipeline_stage writer" grep. Report final test count.
