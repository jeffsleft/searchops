# SearchOps — Quality Metrics & Feature-Usage Tracking Plan

> Companion to `progress-instrumentation-spec-v2.md`. That spec measures the *funnel*
> (is the tool moving the job search forward?). This plan measures the *product*
> (is the tool itself any good, and which parts of it earn their keep?).
> Two different questions — do not merge them.
>
> Status: proposal. Nothing here is built yet. Sequenced so each phase ships value alone.

## The gap this closes

Verified against the codebase (2026-07-04): the app has **zero product-usage instrumentation.**
`task_log` (`app/models.py:201`, written via `log_task_event`) logs *backend jobs* — research,
batch_research, sync — not user behavior. There is no record of which screens Jeff opens, which
features he touches, or which ones he built and then abandoned. So today the honest answer to
"which parts of SearchOps are well used vs. dead weight?" is: **we don't know.** That is the
first thing to fix, because it changes what's worth maintaining.

Constraint that shapes everything below: **this is a single-user app.** Classic product
analytics (DAU, retention cohorts, funnels across thousands of users) are the wrong frame — n=1.
The right frame is **a maintenance-and-attention instrument for one operator**: what do I use,
what's broken, what did I over-build, where do I waste time.

---

## Three metric families

### A. Feature-usage metrics — "what do I actually use?"
The surface is large: ~90 routes in `app/routes.py`, grouped into features (dashboard, discovered,
pipeline, prep/interview, recruiters, targets, settings, calibration). Many were built in bursts
and may be cold. Usage tracking tells you which to keep polishing and which to retire.

- **Feature reach** — distinct features touched per week (are you using the whole tool or 3 screens?).
- **Route hit counts** — per route, per week; the raw signal.
- **Cold features** — routes with zero hits in 30 days that are *not* deprecated. Candidates for
  removal (every dead feature is maintenance + attack surface + `/code-review` noise).
- **Hot paths** — the top 10 routes by volume. These deserve the polish budget and the eval coverage.
- **Action success rate** — for POST routes, ratio of 2xx/3xx to 4xx/5xx. A feature you use that
  keeps erroring is worse than a cold one.

### B. Engine-quality metrics — "is the scoring any good?"
This is where SearchOps proves it's more than a CRUD app. Some of this the v2 spec already
persists (calibration); this extends it.

- **Score→outcome correlation** — do high-scored jobs actually convert to interviews? (the cohort
  question from v2 §5; the single most important quality metric).
- **Score stability** — when a job is re-scored (there's a `/job/{id}/rescore` route and
  `score_history` table), how much does the score move? Large swings on the same JD = an unstable
  rubric. Query `score_history` for variance per job.
- **Auto-reject precision** — of jobs auto-rejected (Layer 1), how many would Jeff have wanted to
  see? Track manual overrides / promotions of auto-rejected jobs as false-positive signal.
- **LLM cost & latency per scoring call** — tokens and wall-time per Gemini call, so cost per
  scored job is a real number (feeds the "~$3/month" claim in CLAUDE.md — verify vs. assert).
- **Corpus coverage** — % of scored jobs where Layer 2 found ≥1 evidence match vs. returned 0.0
  (0.0 means the Accomplishments Inventory didn't cover that role — a content gap, not a bug).

### C. Data-quality / integrity metrics — "is the state trustworthy?"
Cheap to compute, high-signal, and they protect every metric above from silently rotting.

- **Orphan/consistency checks** — jobs with a terminal `pipeline_stage` but no
  `application_outcomes` row (or vice-versa); `pipeline_history` gaps (a stage reached with no
  transition row — directly validates the v2 "single write path" refactor).
- **Staleness** — jobs stuck in a non-terminal stage > N days with no history update (real funnel
  leakage vs. tracking leakage).
- **Ethics-vet backlog** — pipeline companies with the ethics flag still unset (CLAUDE.md requires
  manual confirmation; surface the unconfirmed count).

---

## Implementation — phased, each phase standalone

### Phase 1 — Passive usage capture (the foundation) · ~1 build session
One middleware, one table. The app already has a middleware stack
(`SecurityHeadersMiddleware, CSRFValidationMiddleware, AuthMiddleware` in `app/routes.py:2643`) —
add one more at the end of the request path.

- New table
  `usage_events(id, ts TEXT, method TEXT, route_template TEXT, status INT, duration_ms INT, is_hx INT)`.
  - Store the **route template** (`/job/{job_id}/rescore`), not the resolved path — so
    `/job/417/rescore` and `/job/9/rescore` aggregate. Starlette exposes the matched route;
    read its `.path` in the middleware.
  - `is_hx` = 1 when the `HX-Request` header is present, to separate full-page loads from
    HTMX partials.
  - **No PII, no bodies, no query strings.** Method + template + status + timing only. This keeps
    it safe even though it's single-user, and safe if the repo ever goes public.
- Cost: one INSERT per request. At single-user volume this is trivial. Guard with a
  `USAGE_TRACKING_ENABLED` config flag (default true) so it can be killed instantly.
- **Retention:** a line in the existing monthly cron prunes `usage_events` older than 180 days —
  keeps the table from growing unbounded on a Volume.

### Phase 2 — Feature map + rollups · ~half session
- A static `FEATURE_MAP` dict grouping route templates → feature names (dashboard, discovery,
  pipeline, prep, recruiters, targets, settings, engine). ~90 routes → ~8 features.
- Service `usage_summary(window_days=30)` returning per-feature: hits, distinct-days-used,
  last-used, error-rate; and a `cold_features` list (zero hits in window, not in a
  `DEPRECATED_ROUTES` set).
- Fold the family-B and family-C queries into the same service module
  (`app/services/quality_service.py`) so there's one place metrics live.

### Phase 3 — The "SearchOps Health" view · ~one session
- Route `GET /settings/health` → `templates/health.html`. Internal, not board-facing (that's
  `/settings/progress` from the v2 spec). This one is for Jeff deciding what to build/cut/fix.
- Three panels matching the families:
  - **Usage** — feature heatmap (used this week / cold / never), hot-path top-10, error-rate flags.
  - **Engine quality** — score→outcome cohort chart, score-stability outliers, corpus-coverage %,
    LLM cost-per-scored-job.
  - **Integrity** — the consistency/staleness/ethics-backlog counts, each linking to the affected rows.
- Every number carries its `n` and the same confidence tags as the board view. Same anti-fabrication
  rule: no data → "N/A", never a zero dressed up as a result.

### Phase 4 — Act on it (the payoff) · ongoing
- **Monthly:** the cron appends a one-line usage digest to the snapshot's `raw_json` (cold-feature
  count, hot path, worst error-rate route). Now "which features are dead" is a tracked series, not
  a vibe.
- **Cold-feature decision loop:** any route cold for 60 days triggers a `spawn_task`-style flag:
  keep, fix, or delete. Deleting dead features is the highest-leverage maintenance you can do on a
  solo tool — it shrinks the surface `/code-review`, `pre-ship-security-checklist`, and evals must cover.
- **Feed the engine-quality signals back into the rubric.** If score→outcome correlation is flat,
  the four-layer weights are miscalibrated — that's a `candidate_profile.yaml` tuning task with
  evidence behind it, not guesswork.

---

## What NOT to build (scope discipline)
- **No third-party analytics** (GA, PostHog, Segment). Single-user, password-gated tool; a SQLite
  table is the whole answer. Adding an external beacon would leak the job search to a vendor.
- **No per-user cohorts / retention curves.** n=1. Meaningless and misleading.
- **No client-side event tracking / JS beacons** in phase 1. Server-side route capture answers
  90% of "what do I use" for free. Revisit only if a specific in-page interaction (e.g. which
  dashboard filter) becomes a real question.
- **No vanity counts on the board view.** "1,200 jobs scored" is activity, not outcome — it belongs
  on the internal health view, never the panel-facing one.

## Sequencing vs. the v2 spec
Build order: **v2 spec §1–4 first** (funnel truth + the single write path), then **this Phase 1–2**
(usage capture), then the two views together (**v2 §5 board view + this Phase 3 health view**) since
they share `progress_service` / `quality_service` plumbing and the same confidence-tag component.
The integrity checks (family C) are the cheapest and can slot in anytime after the write-path refactor
lands, because they *test* that refactor.
