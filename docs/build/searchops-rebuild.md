# SearchOps Rebuild — Build Spec (Work Packages)

> Rebuild the private recruiting-engine into **SearchOps**, an open-source, forkable reverse-ATS, then showcase it.
> Strategy/decisions live in the session plan file (kept in the private workspace) and the ADRs (`docs/adr/decisions.md`). This doc is the **executable build spec**: one self-contained work package (WP) per area. Designed on Opus-4.8 Extra; **execute each WP on Sonnet or Opus-4.8 Medium/High — never Extra.**

## How to run a work package

Each WP below is self-contained — any fresh session on any model can execute it from this file alone.

- **Sequential (default):** one session; set the model to the WP's suggested config; say "execute WP-X from docs/build/searchops-rebuild.md"; verify; move on.
- **Parallel (optional):** multiple sessions, each on its own config + its own **git branch** (so they don't edit the same files at once), each pointed at a different WP.

## Conventions (read before any WP)

- **Verify before you change.** This spec names files/patterns from exploration; the executing agent MUST `Read` the actual file before editing — do not trust quoted structure. (Per Jeff's AI_RULES.)
- Follow existing patterns; match surrounding code style. Hand-written SQL (no ORM). HTMX + Jinja2. Modal app id stays `recruiting-engine` during the build.
- Each WP ends with its **Acceptance** + **Verify** met before marking the task complete.
- Personal data never gets committed (see WP-A). Secrets via env/Modal Secret only.
- **Scope discipline.** Execute ONLY the named WP. Do not edit files owned by another WP. If a dependency (see "Depends on") isn't met, **stop and report** — do not build the dependency yourself. Sequential mode: one WP per session run.

## Done-bar (Track B complete when all true)

Scoring is ruthless (an 8+ is rare and earned) · navigation is clear · app feels fast · design is consistent · resume+CL outputs are strong and not AI-sounding · target lists show whether matching roles exist · runs with **no Google setup** · security checklist passes · design refreshed. (Multi-host is the only thing below the bar.)

## Waves & dispatch

| Wave | WP | Area | Config | Depends on |
|---|---|---|---|---|
| 0 | C | Nav reorg | Sonnet Med | — |
| 0 | A | Security baseline (untrack data, gitignore) | Opus Med + sec gate | — |
| 1 | M | Data layer: SQLite default + add-by-URL; Sheets optional | Opus High | — |
| 1 | B | Multi-provider (scoring LLM = any) | Sonnet High | — |
| 1 | J | Scoring methodology overhaul (ruthless) | Opus High | — |
| 1→3 | N | Performance / speed (profile W1, fix through W3) | Opus High | — |
| 2 | D | In-app config (ethics + auto-reject) | Opus High | — |
| 2 | L | Discovery match clarity | Opus High | J, M |
| 2 | E | Resume + cover-letter upgrade | Opus High | — |
| 3 | F | Voice add-on (default + pluggable) | Opus High | E, B |
| 3 | K | UX / navigation overhaul | Opus High + design | C, D, I |
| 3 | I | Design system + refresh (desert vs oceanic) | Opus Med + design | — |
| 4 | G | Docs / examples / LICENSE / CI / demo | Sonnet High | M, D |
| 4 | H | Hosting decouple (stretch) | Opus Med | — |

---

## WP-C — Nav reorg  (Wave 0 · Sonnet Med)
**Goal:** Reorder the left nav. **Read first:** `app/templates/base.html` (nav block).
**Changes:** New order — **top:** Dashboard, Pipeline, Interview Prep. **"Discovery" section:** Discovered, Hunt Targets, Outreach Targets. **"Review":** Ethics Vetting, Auto-rejected, Recruiters. **"Config":** Settings, Methodology, User Guide, Patterns, Bulk Rescore. Update section labels accordingly.
**Acceptance:** nav renders in the new order; `aria-current` highlighting still works; no broken links. **Verify:** load each route. **Depends:** none.

## WP-A — Security baseline  (Wave 0 · Opus Med + gemini-security-reviewer)
**Goal:** Guarantee personal data can't enter the public artifact; harden `.gitignore`. **Read first:** `.gitignore`, `git ls-files`, `candidate_profile.yaml`, `app/discovery/hunt_targets.yaml`, `data/`.
**Changes:** `git rm --cached` the tracked personal files (`candidate_profile.yaml`, `app/discovery/hunt_targets.yaml`, `data/Accomplishments_Inventory.docx`, the two root `*.docx` specs); add them + `data/*.docx` to `.gitignore`; confirm app still loads when those files are absent (fallback or clear error). Secrets audit: confirm no secret values in tracked files. (Example/sanitized configs are produced in WP-G.)
**Acceptance:** `git ls-files` shows zero personal data; app starts without the personal files present; `.gitignore` covers all sensitive patterns. **Verify:** simulate a clean checkout; run `pre-ship-security-checklist`. **Depends:** none.

## WP-M — Data layer: SQLite default + add-by-URL; Sheets optional  (Wave 1 · Opus High)
**Goal:** Make add-by-URL the zero-config default; SQLite is the SSOT; Google Sheets becomes optional. **Read first:** `app/sheets/`, `app/models.py`, intake/discovered routes, `app/config.py` (`GOOGLE_SHEET_ID`).
**Changes:** Gate all Sheets calls behind "is Google configured?" (e.g., `GOOGLE_SHEET_ID` + creds present); ensure the add-by-URL path persists to SQLite as canonical; when Sheets is configured, treat it as optional push/pull, never required; app must start + run core flows with no Google creds.
**Acceptance:** with `GOOGLE_SHEET_ID` unset and no creds, app boots and add-by-URL → score → appears in Discovered/Pipeline works end-to-end. **Verify:** run with Google unset; add a job by URL. **Depends:** none (foundational).

## WP-B — Multi-provider (scoring LLM = any)  (Wave 1 · Sonnet High)
**Goal:** Env-selectable LLM provider; add OpenAI. **Read first:** `app/providers/__init__.py`, `gemini.py`, `anthropic_provider.py`, `app/config.py`.
**Changes:** Refactor `get_provider()` to read `LLM_PROVIDER` (`gemini|anthropic|openai`) and build the primary; keep `FallbackProvider` chain. Add `app/providers/openai_provider.py` implementing `generate` / `generate_json` / `name` (mirror `anthropic_provider.py`). Add `OPENAI_API_KEY` + `OPENAI_MODEL` env. Document BYO-key.
**Acceptance:** switching `LLM_PROVIDER` switches provider; a scoring call succeeds under each provider given its key; fallback still works. **Verify:** scoring pass under each provider. **Depends:** none.

## WP-J — Scoring methodology overhaul (ruthless)  (Wave 1 · Opus High)
**Goal:** Ruthless, discriminating scores — an 8+ is rare and earned. **Read first:** `app/scoring/engine.py` (confirm exact L4 summation + that there's a single final clamp), `match.py`, `research.py`, `prompts.py`, `candidate_profile.yaml`, the calibration view.
**Diagnosis:** base 5.0 + additive L4 positives (e.g., AI +2.0, DevTools +1.5, remote +1.0, modern stack +1.5 = +6) overwhelm the ceiling before Layer 2 (actual fit) matters → glut of false 8–10s.
**Changes:** lower base (~3.5); cap total L4 positive contribution + firm up negatives; make **Layer 2 the decisive layer** (widen range; prompts demand *specific* JD-requirement→accomplishment matches, penalize generic); implement a **top-band gate** — cap final at **7.0** unless ALL must-haves hold: comp on target trajectory · genuine leadership role (not IC) · acceptable/preferred sector · real build/strategic mandate · strong specific Layer-2 evidence. Make base/caps/gate configurable in `candidate_profile.yaml`. (Gate is the proposed set — tune with Jeff.)
**Acceptance:** bulk-rescore of the existing set spreads (most 3–6, 7–8 occasional, 9–10 rare); known false "home runs" now ≤7; calibration view shows better separation. **Verify:** bulk rescore + inspect distribution + backtest vs calibration. **Depends:** none (pairs with L).

## WP-N — Performance / speed  (Wave 1→3 · Opus High)
**Goal:** App feels fast. **Read first:** `app/main.py`, `app/routes.py`, `app/models.py`, `app/scoring/*`.
**Changes:** profile hot paths (dashboard/page loads, scoring latency, repeated LLM calls, DB queries); add DB indexes; cache where safe (extend existing score/research cache); parallelize independent LLM calls; cut redundant round-trips; lazy-load heavy views.
**Acceptance:** measurable latency drop on dashboard + scoring, no regressions. **Verify:** time key flows before/after. **Depends:** profile in Wave 1, apply fixes through Wave 3.

## WP-D — In-app config: ethics + auto-reject  (Wave 2 · Opus High)
**Goal:** Edit ethics-vetting reasons + auto-reject no-go industries in the app. **Read first:** `app/templates/settings.html` + its routes and the existing DB-backed `title_filters` / `candidate_settings` pattern, `app/models.py`, `app/scoring/engine.py` (`_HARD_NO_KEYWORDS`), `candidate_profile.yaml` (ethics `hard_no`, auto-reject rules).
**Changes:** add DB-backed storage for ethics reasons + no-go industries/keywords (seed from YAML on first run), with add/remove/enable UI following the existing settings pattern exactly; engine reads from DB (fallback YAML).
**Acceptance:** add/remove a no-go industry + an ethics reason in-app; persists across redeploy; auto-reject uses the DB list. **Verify:** add a no-go sector → matching job auto-rejected; remove → not. **Depends:** coordinate with WP-J (auto-reject lives in engine).

## WP-L — Discovery match clarity  (Wave 2 · Opus High)
**Goal:** On Hunt Targets + Outreach Targets, show whether a company has matching open roles. **Read first:** `app/discovery/` (ATS clients, hunter, matcher), `/targets` + `/companies` routes/templates, models.
**Changes:** per target, fetch/refresh open roles (ATS clients), run the matcher against the profile, store a match summary (count of matching roles + best score); surface a badge/column on both target lists ("3 roles match · best 8.2" vs "no current match"); link through to the matched jobs; refreshable.
**Acceptance:** target lists show match status; clicking reveals the matching roles; refresh updates it. **Verify:** add a target with a known fitting role; confirm it shows matches. **Depends:** J (scores), M (data).

## WP-E — Resume + cover-letter upgrade  (Wave 2 · Opus High)
**Goal:** Content↔design split (stop breaking formatting) + stronger outputs + full cover letter. **Read first:** `app/resume_docx.py` (currently rebuilds the doc from scratch — the bug), where resume `sections`/data originate (`/job/{id}/resume`), the `cover_letter_hooks` in prompts, and the candidate's master resume templates (maintained outside the repo) for reference.
**Changes:** replace from-scratch build with **docxtpl + a locked master `.docx` template**; engine emits structured content only; ship a **generic default master resume + cover-letter template** in the repo; generate a **full tailored cover letter** (not just hooks); strengthen JD→accomplishment tailoring quality. Document the design skill a user adds to build their own template.
**Acceptance:** tailoring the same resume twice → design byte-stable except swapped content; cover letter is a full tailored letter; outputs read strong (and pass WP-F voice gate). **Verify:** generate resume + CL for a sample job; diff design stability; read for quality. **Depends:** pairs with F.

## WP-F — Voice add-on (default + pluggable)  (Wave 3 · Opus High)
**Goal:** Optional de-AI voice module for resume/CL prose; works out of the box, pluggable. **Read first:** `~/Projects/voice-engine/` (`gate/`, `subagents/`, `constraints/`), WP-E output path.
**Changes:** vendor a minimal voice module (Inspector/Weaver/Critic/Miller gates + a generic "don't sound like AI" forbidden-words default) that post-processes generated prose; config to point at the user's own style guide/corpus; **ships a working default**; UI **surfaces which guide is active**; fully optional (off → generation still works).
**Acceptance:** default-on outputs avoid AI-tells; user can supply own style guide; UI shows active guide; disabling doesn't break generation. **Verify:** generate a CL voice on/off; swap style guide. **Depends:** E, B.

## WP-K — UX / navigation overhaul  (Wave 3 · Opus High + design skills)
**Goal:** Clearer, faster-feeling, consistent flows. **Read first:** templates across `app/templates/`, the core flow (find → score → review → act); coordinate with WP-I + WP-N.
**Changes:** audit + reduce friction in the core flow (fewer clicks, clearer states), fix small inconsistencies (labels, spacing, empty/loading states), improve drawer + dashboard usability, ensure consistent components.
**Acceptance:** core flows take fewer steps; components consistent; no obvious inconsistencies. **Verify:** walk the flows; run `ship-check`. **Depends:** C, D, I.

## WP-I — Design system + refresh (desert vs. oceanic)  (Wave 3 · Opus Med + design skills)
**Goal:** Consistent design tokens + a refreshed palette off the current brown. **Read first:** `app/static/css/styles.css`, `app/templates/base.html` (CSS vars: `data-theme`, `--accent`, `--bg-elev`, `--border`).
**Changes:** design-system pass first (consistent tokens/components — spacing, type, buttons, cards); prototype **Desert** (warm sand/clay/sage) and **Oceanic** (cool blue/teal/seafoam) palettes as `show_widget` mockups; pick one; add light theme; apply as CSS-variable tokens. Capture before/after for the case study.
**Acceptance:** consistent look; chosen palette applied; light/dark works; WCAG AA contrast. **Verify:** visual review across pages. **Depends:** pairs with K.

## WP-G — Docs / examples / LICENSE / CI / demo  (Wave 4 · Sonnet High)
**Goal:** Forkable + documented to the "a stranger can run it" bar. **Read first:** final state after WP-M + WP-D (config shape).
**Changes:** rewrite `README.md` (**give-back intro**, install/configure/run local+Modal, baseline job-finding method, how-to-use, screenshots/GIF, the three intake modes — Hunt Targets / Add-by-URL (default) / Outreach Targets); generic `AGENTS.md` (works with Claude Code / Gemini CLI / OpenAI Codex); `LICENSE` (MIT); `CONTRIBUTING.md` + issue templates; `.env.example`, sanitized `candidate_profile.example.yaml` + `hunt_targets.example.yaml`; **demo dataset + seed profile** (fictional); **CI (GitHub Actions)**: lint + tests + a provider-switch test.
**Acceptance:** fresh-clone test passes (clone to temp dir, follow README, it runs); CI green. **Verify:** the fresh-clone test. **Depends:** M, D.

## WP-H — Hosting decouple (stretch)  (Wave 4 · Opus Med)
**Goal:** Can run beyond Modal. **Read first:** `app/main.py` (Modal coupling isolated here), `app/config.py`.
**Changes:** extract the FastAPI/ASGI app from Modal-specific code (e.g. `app/asgi.py`); env-based secrets; document Vercel / Cloudflare / container options + DB portability (Volume SQLite → D1/Postgres). Keep Modal as the reference deploy.
**Acceptance:** ASGI app runs without Modal; `modal deploy` still works; docs written. **Verify:** run ASGI locally without Modal; confirm Modal deploy unaffected. **Depends:** late; below the done-bar.
