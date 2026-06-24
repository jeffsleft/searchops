# SearchOps — Architecture Decision Records

Concise ADRs for code-level decisions. Strategic decisions live in the Apple Notes "CoS — Decision Log".

## ADR-0001 — Ruthless scoring methodology
**Context:** too many jobs score 8+ (false "home runs"). Base 5.0 + additive L4 positives overwhelm the ceiling before L2 (actual fit) matters.
**Decision:** lower base (~3.5); cap total L4 positive contribution; firm up negatives; make L2 the decisive layer (demand specific JD→accomplishment evidence); add a top-band gate (cap final at 7.0 unless must-haves met: comp trajectory, leadership role, acceptable sector, build mandate, strong L2 evidence). Make it configurable in `candidate_profile.yaml`.
**Status:** accepted · impl WP-J. Gate must-haves are provisional — tune with Jeff.

## ADR-0002 — Env-selectable LLM provider
**Decision:** `get_provider()` reads `LLM_PROVIDER` (gemini|anthropic|openai); `FallbackProvider` for resilience; add `OpenAIProvider` mirroring the existing interface. Bring-your-own-key.
**Status:** accepted · impl WP-B.

## ADR-0003 — SQLite SSOT, Sheets optional, add-by-URL default
**Context:** Google Sheets OAuth is the biggest forker onboarding barrier.
**Decision:** SQLite is canonical; add-by-URL is the default intake; Sheets is optional sync when configured; app boots with no Google creds.
**Status:** accepted · impl WP-M.

## ADR-0004 — Single-user auth; multi-user deferred
**Decision:** keep session-cookie auth + per-IP throttle. Document the multi-user boundary as a known limitation; build no accounts now. If it ever goes multi-user/hosted, add a Worker + real auth.
**Status:** accepted.

## ADR-0005 — Resume/cover-letter content↔design split
**Context:** `app/resume_docx.py` rebuilds the doc from scratch, so tailoring breaks the design.
**Decision:** engine emits structured content only; a locked docxtpl master template renders it; ship generic default templates.
**Status:** accepted · impl WP-E.

## ADR-0006 — Go public via a fresh sanitized snapshot
**Decision:** build in place (Modal app id stays `recruiting-engine`); at showcase, create a fresh public repo from a sanitized snapshot so personal data never enters public history.
**Status:** accepted.

## ADR-0007 — Design refresh inside Track B
**Decision:** the UI is part of the proof (case-study screenshots), so the design-system pass + palette refresh (desert vs. oceanic) happen before the showcase, not deferred.
**Status:** accepted · impl WP-I.
