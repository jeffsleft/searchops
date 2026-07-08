# Claude Design handoff — SearchOps metrics dashboards

Paste the block below into Claude Design. It restyles two **already-built, already-working**
Jinja templates — the backend, routes, and data are done and must not change. Scope is visual
design + markup only.

Templates to restyle:
- `app/templates/settings_progress.html` (route `/settings/progress`)
- `app/templates/settings_health.html` (route `/settings/health`)

---

```
You are restyling two internal reporting dashboards for "SearchOps," a personal job-search
analytics tool (single user, dark-first, data-dense, operator-facing — a RevOps console, not a
marketing page). The backend is DONE and must not change: two Starlette routes already render
Jinja templates from fixed context variables. Restyle the visual design and markup of those two
templates ONLY — CSS (app/static/css/styles.css) and the HTML in the two template files. Do NOT
propose backend, route, service, or data changes.

EXISTING DESIGN SYSTEM — match it exactly, introduce no framework:
- Pure custom CSS in app/static/css/styles.css. NO Tailwind, NO Bootstrap, NO chart library
  is loaded (base.html loads only htmx). If a sparkline needs SVG, inline it.
- Color: OKLCH CSS variables with a dark + light mode. Use ONLY these tokens, never hardcode:
  --bg, --bg-elev, --bg-sunk, --bg-hover, --border, --border-strong,
  --text, --text-muted, --text-dim, --accent (sage green), --accent-soft,
  score tiers --tier-dream/-solid/-worth/-skip/-pass, --rust (warnings).
  Deliver light mode via the existing token swap — do not restyle per-mode by hand.
- Type: Inter (UI), JetBrains Mono (all numbers — always font-variant-numeric: tabular-nums),
  Source Serif 4 (rare). Spacing/shape: --pad 16px, --radius 5px, --radius-lg 8px, --row-h 34px.
- Reuse these existing component classes: .card / .card-head (uppercase 11px label) / .card-body;
  .stat-grid (4-col) with .stat / .stat-label / .stat-value (28px mono); .tbl (sticky uppercase
  th, hover rows); .score-badge.tier-* with a .dot; .score-bar (flex row of colored stacked
  spans — the existing mini-bar idiom, good for a sparkline).
- Existing helper classes already added for these pages (restyle freely): .conf /
  .conf-solid / .conf-directional / .conf-insufficient (confidence chips); .metric-na,
  .metric-n, .metric-big; .usage-state / .usage-active / .usage-cold / .usage-never; .spark.
- Layout shell: 220px sidebar + main; content max-width 1480px; page padding 24px 32px. Both
  pages extend base.html and fill {% block content %}.

SCREEN 1 — /settings/progress (settings_progress.html) — BOARD-FACING funnel view.
The artifact Jeff screenshots for a hiring panel. Must read like a RevOps leader's funnel report.
Context variable: `progress`, with this exact shape:
  progress = {
    funnel_trend: [ {month:"YYYY-MM", applied:int, screen:int, interview:int, offer:int}, ... ],
    conversion:  [ {stage:str, rate:float|None, n:int, confidence:"solid"|"directional"|"insufficient"}, ... ],
    velocity:    { days_to_first_interview: {value:float|None, n:int, confidence:str} },
    targeting:   { interviews_per_10_apps: {value:float|None, n:int, confidence:str},
                   cohort: [ {band:str, rate:float|None, n:int}, ... ] },
    calibration: { insufficient_data:bool, buckets: [ {label:str, rate:float|None, n:int}, ... ] }
  }
Sections: (a) Funnel trend — applied→screen→interview→offer as a TREND across funnel_trend
months (sparkline / small-multiple), plus current rates; (b) Stage conversion — each rate with
its n and confidence chip; (c) Velocity — median days to first interview; (d) Targeting quality —
interviews per 10 applications + the score-band cohort (does score≥8 convert better than 5–8?);
(e) Model calibration — per-probability-bucket actual interview rate.

SCREEN 2 — /settings/health (settings_health.html) — INTERNAL ops view. Denser, utilitarian.
Four context variables, exact shapes:
  usage = { window_days:int, total_events:int, cold_features:[str],
            features:[ {name:str, state:"active"|"cold"|"never", hits:int, last_used:str|None, error_rate:float} ],
            hot_paths:[ {route:str, hits:int} ] }
  system = { window_days:int, req_count:int, error_count:int, error_rate:float,
             p50_ms:int|None, p95_ms:int|None,
             crons:[ {task_type:str, last_ok:str|None} ],
             recent_errors:[ {ts:str, route_template:str, exc_type:str, message:str} ] }
  engine = { score_cohort:[ {band:str, n:int, interview_rate:float|None, confidence:str} ],
             score_stability:{value:float|None, n:int, confidence:str},
             corpus_coverage:{value:float|None, n:int, confidence:str} }
  integrity = { checks:[ {check:str, count:int, href:str} ] }
Sections: (a) Usage — feature heatmap (active / cold / never), hot-paths top-10, error-rate
flags, cold-features callout; (b) System — request volume, error rate, p50/p95 latency, last
success per background job, recent errors; (c) Engine quality — score→outcome cohort, score
stability, corpus coverage; (d) Data integrity — the checks, each linking to its href, count in
--rust when > 0.

HARD RULES:
- Never render a percentage or scalar without its n beside it. When a value is null / confidence
  is "insufficient", render "Insufficient data — n=X" — a first-class DESIGNED state, never a
  blank or a fake 0. Design all three confidence states (solid / directional / insufficient).
- Dark mode is primary. No external fonts / CDNs / JS beyond what base.html already loads.
- Deliverable: the two updated Jinja templates + any additions to styles.css, using the tokens
  and component classes above. Provide a desktop mock of each screen showing all confidence states.
```
