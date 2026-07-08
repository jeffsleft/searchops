# [ARCHIVED 2026-07-08] Outcome Charter: Routes.py Refactor

> **Status at archive time:** KR1 (scoring SQL consolidated to `app/services/scoring_service.py`)
> met via the 2026-06-11 audit and follow-on sessions; the last bypass
> (`job_promote_from_discovery`) closed 2026-07-08. KR3 (identical behavior, Modal
> deploys clean) met. KR2 (all handlers ≤40 lines) **not met and consciously dropped** —
> Session 40 (2026-06-22) ruled further routes.py thinning discretionary polish, not a gap.
> This was a session-scoped charter that lived in `outcome_charter.md` longer than it
> should have; archived here as a record of the ODF process. The live charter now covers
> the use phase of the project.

_Owned by outcome-architect. Last updated: 2026-06-01_

## Definition of Done

Scoring and discovery business logic extracted from route handlers into service modules, such that a bug fix in one caller (e.g., `job_paste_and_score`) is automatically available to all other callers (e.g., `job_rescore`, `linkedin_add`) without manual duplication.

## Objective

Reduce the cost of bug fixes and feature changes to the scoring and discovery pipelines by eliminating code duplication across route handlers. After refactor, changes to "score a job" or "scan a company" happen once in a service module, not 4+ times across routes.

## Key Results

| Key Result | Metric | Threshold | Timeframe |
|---|---|---|---|
| KR1: Scoring duplication eliminated | Identical SQL INSERT/UPDATE blocks in `job_fetch_and_score`, `job_paste_and_score`, `job_rescore`, `linkedin_add` | 0 duplicates (identical blocks extracted to `app/services/scoring_service.py`) | By end of session |
| KR2: Route handlers thinned | Max lines in a route handler | ≤ 40 lines per handler (excluding docstring + imports; currently 50–95) | By end of session |
| KR3: No user-visible behavior change | Functional test pass rate | 100% of existing manual tests pass (if available); smoke test: dashboard loads, score a job workflow, discover new jobs, research a company all work exactly as before | By end of session |

## Current State (Baseline)

**Scoring duplication:**
- `job_fetch_and_score` (lines 1906–1968): Calls `score_job()`, builds identical UPDATE statement, handles HIGH_SCORE_THRESHOLD alert, returns fragment
- `job_paste_and_score` (lines 1971–2061): Calls `score_job()`, builds identical UPDATE statement (with identical 20+ fields), handles alert, returns fragment
- `job_rescore` (lines 2064–2145): Calls `score_job()`, builds identical UPDATE, handles alert, returns fragment
- `linkedin_add` (lines 2148–2230): Calls `score_job()`, builds identical UPDATE, handles alert, returns fragment

**Identical pattern repeated 4 times:** Parse input → validate → call `score_job()` → map result to 20+ SQL params → execute UPDATE → check HIGH_SCORE_THRESHOLD → send Slack alert if score ≥ 8 → return HTML fragment

**Bug impact:** A fix to the SQL UPDATE statement (e.g., missing field, wrong type coercion, Slack alert condition) must be applied 4 places. Current state: bug fix in one route leaves 3 others with the same bug until manually fixed.

**Discovery duplication (partial extraction):**
- `do_scan_company()` exists in `app/services/discovery_service.py` but is called directly from `targets_scan_now` route
- `api_discovery_scan` calls `run_discovery_scan()` instead, a separate entry point (minimal duplication here)
- Root issue: `targets_scan_now` wraps the scan call in HTML rendering logic + error handling; this wrap logic is not extracted

## Ontology Map

| Term | Explicit Definition |
|---|---|
| **Duplication eliminated** | Identical code blocks (same lines of logic) appear in ≤ 1 place. Specifically: the SQL UPDATE statement for job scoring and the Slack alert condition must each appear exactly once in `app/services/scoring_service.py`, not 4 times in routes. |
| **Thin route handler** | ≤ 40 lines of actual logic (excl. docstring, imports, type hints). Pattern: extract params → validate → call service → map response to HTML → return. No business logic inside the handler. |
| **No user-visible change** | The UI fragment returned, the database state after the operation, and the Slack alert fired (if applicable) are identical before and after refactor. A user running the same workflow (score a job, rescore, discover, alert) sees no difference. |
| **Maintainability improved** | Measured by: bug fix to scoring logic requires change in 1 place, not 4. Feature addition (e.g., new flag to track in the UPDATE) requires change in 1 place. |

## Critical Constraints

1. **No new dependencies** — only use stdlib and existing packages (`sqlite3`, `jinja2`, `starlette`, etc.)
2. **No Modal changes** — app must deploy without modification to `app/main.py` or env config
3. **Backward compatibility** — all existing routes must return identical HTML/JSON/status codes; no route path changes
4. **Single-threaded execution** — no async refactoring (already handled by routes via `asyncio.to_thread`); service modules remain sync
5. **SQLite only** — no switch to ORM or stored procedures; hand-written SQL stays in service layer

## Blocker Analysis: The 2–3 Real Constraints That Unlock Progress

### Blocker 1: Score-and-Insert Pattern Extraction
**The work:** Extract the 20+ field SQL UPDATE into a service function `persist_score_to_db(job_id: int, score_record: dict) → None`. This function must:
- Accept the score output dict from `score_job()`
- Map all fields (final_score, deterministic_score, llm_adjustment, tech_stack_json, evidence, mismatches, bullets, hooks, etc.) to SQL params
- Execute the identical UPDATE statement currently in 4 places
- Handle the Slack alert check (if final_score >= HIGH_SCORE_THRESHOLD)

**Why it blocks:** Until this is extracted, the 4 routes will continue to duplicate the 40-line UPDATE block. All 4 routes depend on this.

### Blocker 2: Route Consolidation
**The work:** After `persist_score_to_db()` exists, refactor the 4 scoring routes to:
- Call `persist_score_to_db(job_id, score_record)` instead of inline SQL
- Reduce to ~20 lines: parse → validate → score → persist → return fragment

**Why it blocks:** Routes won't use the extracted service until they're refactored. Without this, the extraction is dead code.

### Blocker 3: Discovery Route Consolidation
**The work:** Extract the error-handling and HTML-rendering wrap around `do_scan_company()` into a service function `scan_company_with_result(co_id: int) → dict`. This function should:
- Call `do_scan_company()` (already extracted)
- Handle exceptions, update last_scanned timestamp, render status HTML
- Return HTML fragment (not raise)

**Why it blocks:** `targets_scan_now` and other discovery routes will continue to duplicate error handling and rendering logic until this is extracted. Lower priority than Blocker 1 (fewer routes affected, but same pattern).

## Stakeholder Layer

- **Executive sponsor accountability:** Jeff (solo dev, self-interested in maintainability)
- **Known resistance points:** None anticipated (internal tool, no users affected by intermediate states, solo dev = full autonomy)
- **What failure means:** Bug fixes and feature additions to scoring/discovery pipelines remain scattered across 4+ handlers; future changes take 2–3x longer than necessary because the same fix must be applied in parallel across routes

## Authorization

```
/goal Extract scoring and discovery business logic from routes.py into service modules, 
       eliminating code duplication across 4+ route handlers.
       | KR1: 0 duplicate SQL INSERT/UPDATE blocks for scoring (consolidated to service_scoring.py) 
       | KR2: Route handlers reduced to ≤40 lines; smoke test passes (dashboard, score, discover, research workflows unchanged)
       | KR3: Modal deploys without config changes; all existing routes return identical responses
```

**gemini-builder is authorized to begin execution after this block is present.**

---

## Implementation Notes (for reference, not part of charter)

**New service modules to create:**
- `app/services/scoring_service.py` — contains `persist_score_to_db(job_id, score_record)`

**Existing service modules to extend:**
- `app/services/discovery_service.py` — add `scan_company_with_result(co_id)` wrapper

**Routes to refactor (in order of priority):**
1. `job_paste_and_score` (most complex; establishes pattern)
2. `job_fetch_and_score` (similar pattern, fewer validation steps)
3. `job_rescore` (simplest; validates the pattern)
4. `linkedin_add` (variant; may require conditional logic in service)
5. `targets_scan_now` (discovery; lower priority, fewer callers)

**Test strategy:**
- No new tests required (single-user internal tool); use manual smoke test before deploy
- Verify: (a) score a job via paste, (b) score via fetch (if available), (c) rescore, (d) check Slack alert fires at score ≥ 8
- Verify: (a) discover new jobs, (b) rescan company, (c) check last_scanned updates

---

_This charter defines done in business language. The project plan will detail tactical execution steps and file-by-file changes._
