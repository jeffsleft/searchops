# SearchOps — Progress Instrumentation & Self-Reporting Layer

> Implementation spec. Hand to `gemini-builder` (per AI_RULES delegation) or run in a
> fresh Claude Code session. Written to be executed as-is by an AI builder.
> Author intent: capture measurable outcomes as they occur so career-progress metrics
> are derived from real rows — never reconstructed by hand — and feed them into the
> `/wins-rollup` skill's outcome grading automatically.

## Context
You are working in the `recruiting-engine` repo (SearchOps): Modal + FastAPI +
raw sqlite3 (no ORM) + HTMX/Jinja2, deployed on a Modal Volume. Read `CLAUDE.md` and
`~/Projects/AI_RULES.md` (§3 Modal, §6 SQLite, §9 Python) before writing code. The app
already has tables: `jobs`, `pipeline_history`, `application_outcomes`, `score_history`,
`interview_sessions`. Confirm their real schemas with `.schema` before touching them —
do not assume column names.

## Objective
Add a progress-tracking layer that CAPTURES measurable outcomes as they occur and
SELF-REPORTS them monthly, so career-progress metrics are derived from real rows —
never reconstructed by hand. The output must be honest: any metric with no underlying
data is emitted as "N/A (no data yet)", never as a fabricated number.

## Scope — build exactly these four pieces. Do not expand scope.

### 1. Backfill baselines (one-time, idempotent)
- New table `progress_baselines(id, metric_key TEXT UNIQUE, label TEXT, value TEXT,
  captured_at TEXT, source TEXT)`.
- Seed it (INSERT OR IGNORE) with the already-known deltas so future work measures
  against them:
  - `index_speedup` = "19-232x (bench_indexes.py, 20k/5k)"
  - `sheets_removal` = "net -1056 LOC / 3 deps (d5aeea7)"
  - `test_count` = "63"
  - `db_tables` = "25"
  - `source` cites the proof (commit/PR/file) for each.
- New table `milestones(id, name TEXT, completed_on TEXT, evidence TEXT)`; seed the
  Track A/B work-package + public-launch + case-study milestones from `docs/wins.md`.
  Idempotent on (name).

### 2. Funnel + calibration instrumentation (the live capture)
- Find every code path that changes a job's `pipeline_stage`. At each, also write a
  timestamped `pipeline_history` row (job_id, from_stage, to_stage, changed_at) if that
  isn't already happening. Make it a single helper so there is ONE write path.
- On the terminal transitions (applied / screen / interview / offer / rejected_them /
  rejected_me / ghosted), also upsert the corresponding `application_outcomes` row.
- Add a `calibration_summary()` service function that reads `jobs.interview_probability`
  vs. actual `application_outcomes` and returns: `n_scored`, `n_with_outcome`,
  `hit_rate`, `mean_brier`. It MUST return explicit nulls + `insufficient_data: true`
  when `n_with_outcome` < a configurable minimum (default 5). No metric on thin data.

### 3. Monthly snapshot cron (the delta engine)
- New table `progress_snapshots(id, snapshot_date TEXT UNIQUE, jobs_scored INT,
  companies_covered INT, apps_sent INT, interviews INT, offers INT,
  calibration_hit_rate REAL NULL, calibration_brier REAL NULL, raw_json TEXT)`.
- A Modal cron (1st of month, 09:00 UTC — match the existing cron style in the repo)
  computes each field from live tables and writes one row. `raw_json` stores the full
  computed blob for auditability. Idempotent on `snapshot_date` (INSERT OR REPLACE).
- Every numeric field derives from a COUNT/AVG over real rows. If a source table is
  empty, the field is 0 or NULL — never invented.

### 4. Auto-emit a graded wins.md entry (closes the loop)
- After writing a snapshot, compute the delta vs. the previous snapshot row and append
  ONE entry to `docs/wins.md` using this exact template — filling baseline from the
  prior snapshot and result from the current one:

  ```
  ## SearchOps progress snapshot — {snapshot_date}
  - **Objective:** demonstrate the tool drives a real job-search funnel
  - **Baseline:** {prior snapshot values, or "first snapshot — no baseline"}
  - **Result:** {current values + deltas}
  - **Proof:** progress_snapshots row {snapshot_date}; /settings/calibration
  ```

- If this is the first snapshot, write "first snapshot — baseline established" instead
  of inventing a delta. Append only; never rewrite existing `wins.md` content.

## Customization (config-driven, not hardcoded)
Expose in app config (env or a settings row): the calibration minimum-sample threshold,
the cron schedule, and a boolean `WINS_AUTOEMIT_ENABLED` (default true) so the wins.md
auto-append can be turned off without removing the snapshot.

## Constraints & guardrails
- Raw sqlite3, parameterized queries only. No ORM. Match existing repo patterns.
- All new tables created via the app's existing `init_db()`/migration path, idempotently.
- Additive only: do not alter existing table schemas or delete columns. If a needed
  column is genuinely missing, add it with a nullable ALTER, guarded by a
  "column exists?" check.
- ANTI-FABRICATION (non-negotiable): no computed metric may be surfaced or written
  unless it is backed by real rows. Empty source → 0 / NULL / "N/A", explicitly.
- Do not deploy. Stop at: schema + services + cron wired + tests green, and report a
  diff summary. Deployment is a separate human-gated step (AI_RULES §3).

## Acceptance criteria
1. Fresh `init_db()` creates all four tables idempotently (run twice, no error).
2. A stage change on a test job writes exactly one `pipeline_history` row and the
   matching `application_outcomes` row.
3. `calibration_summary()` returns `insufficient_data: true` on <5 outcomes, real
   numbers on ≥5 (prove with a seeded fixture).
4. Running the snapshot job twice on the same date yields one row, not two.
5. First `wins.md` auto-entry says "baseline established"; a second (simulated next
   month) shows a real delta. `wins.md` content above the new entry is untouched.
6. Unit tests cover the calibration thin-data branch and the snapshot idempotency.
   Report final test count.
