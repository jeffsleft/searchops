# Scoring Engine Specification

> This document is the implementation contract for the scoring engine. If the
> engine code disagrees with this doc, fix the code. The in-app methodology page
> at `/settings/methodology` (template: `app/templates/settings_methodology.html`)
> is the human-facing version of the same model.

## Overview

The Reverse ATS scoring engine evaluates job opportunities against Jeff's profile
and full accomplishments corpus. It runs in **four layers, in order**, with a
**single clamp at the end**.

```
final = clamp_0_10( base (5.0) + Layer 4 + Layer 2 + Layer 3 )
```

If Layer 1 (Auto-Reject) triggers, the rest of the pipeline is skipped and
`final = 0.0`.

| # | Layer | Type | Range | Code |
|---|---|---|---|---|
| 1 | Auto-Reject | Deterministic | → 0.0 | `app/scoring/engine.py::check_auto_reject` |
| 2 | Match to Candidate | LLM + corpus | −3.0 to +3.0 | `app/scoring/match.py::score_match` |
| 3 | LLM Qualitative | LLM | −1.0 to +1.0 | `app/scoring/research.py::score_job` + `SCORING_PROMPT` |
| 4 | Adjustment Weights | Deterministic substrings | additive | `app/scoring/engine.py::calculate_adjustment_weights` |

Orchestrator: `app/scoring/research.py::score_job()`.
Composition + final clamp: `app/scoring/engine.py::compute_final_score()`.

---

## 1. Input

The engine accepts one of three input types:

- **URL** — fetched via BeautifulSoup, truncated to 10k chars.
- **Pasted text** — raw JD text.
- **Bulk CSV** — `company`, `job_title`, `url` rows; sequential with rate limiting.

---

## 2. Layer 1 — Auto-Reject

Loaded from `candidate_profile.yaml`. Runs first. No API calls.

### 2a. Salary floor

`compensation.base_min` from the profile (currently $175,000). Salary is extracted
via regex from the JD; the lowest matched figure is used. **If no salary is detected,
the role is NOT rejected** — benefit of the doubt.

### 2b. Blocked sectors

Case-insensitive substring match against the JD. Triggers in
`engine.py::_HARD_NO_KEYWORDS`:

- Healthcare / HealthTech
- Government / Public Sector
- Utilities
- Adult / Pornography
- Gambling
- Predatory Lending
- Hate / Racism

Any hit → `final = 0.0`, layers 2-4 are skipped.

---

## 3. Layer 2 — Match to Candidate (highest weight)

**Range**: −3.0 to +3.0
**Cost**: one Gemini call per scored job
**Prompt**: `app/scoring/prompts.py::MATCH_PROMPT`
**Inputs**:
1. Candidate summary (`_candidate_summary()` from `research.py`)
2. Candidate corpus (parsed `data/Accomplishments_Inventory.docx` via `corpus.py`)
3. JD text (capped at 10k chars)

### 3a. Corpus structure (Cowork's deliverable)

`data/Accomplishments_Inventory.docx`:

- `Heading 1` = company / role section (e.g., "GitLab — Sr. Manager CS Ops")
- `Heading 2` = theme inside that role (e.g., "Renewal Operations")
- bullets = accomplishments, with optional trailing `#hashtag` tags
- top-level `Differentiators` H1 section captures the moats

Parser is `app/scoring/corpus.py::load_corpus()`. If the file is missing,
returns `{"available": False, ...}` and Layer 2 contributes 0.0 with a
high-severity mismatch entry.

### 3b. Output schema

```json
{
  "match_score": <float in [-3.0, +3.0]>,
  "match_summary": "<2-3 sentences>",
  "evidence": [
    {"jd_requirement": "...", "matched_accomplishment": "...", "strength": "Strong|Moderate|Weak"}
  ],
  "mismatches": [
    {"jd_requirement": "...", "gap": "...", "severity": "High|Medium|Low"}
  ],
  "differentiator_themes": ["..."],
  "tailored_bullets": ["3-5 bullets pulled directly from the corpus"],
  "cover_letter_hooks": ["1-3 lines for the cover letter"]
}
```

### 3c. Scoring scale

| match_score | Meaning |
|---|---|
| +2.5 to +3.0 | Direct, recent, scaled evidence. Multiple Strong matches, no High-severity gaps. |
| +1.0 to +2.4 | Solid partial match. Key responsibilities have evidence at smaller scale or adjacent domain. |
| −0.9 to +0.9 | Mixed. Some evidence, meaningful gaps. |
| −2.4 to −1.0 | Mostly mismatch. Core requirements lack corpus support. |
| −3.0 to −2.5 | Fundamental mismatch — never done at any scale in any related domain. |

Score is `_clamp(score, -3.0, 3.0)` in `match.py` before being returned.

---

## 4. Layer 3 — LLM Qualitative Adjustment

**Range**: −1.0 to +1.0
**Cost**: one Gemini call per scored job
**Prompt**: `app/scoring/prompts.py::SCORING_PROMPT`
**Job**: role-shape vibe check — IC vs leadership balance, authority, reporting
line, strategy-vs-execution skew, plus pros/cons, tech stack, pricing, sector,
recommended angle, salary, FDE-model detection.

Output includes a `role_shape` block (`ic_vs_leadership`, `team_size_to_lead`,
`reporting_line`, `strategic_vs_execution`) for display purposes.

---

## 5. Layer 4 — Adjustment Weights (deterministic)

Substring keyword matching on the JD, plus a few flags from company research.
Lives in `engine.py::calculate_adjustment_weights()`. No clamping inside the
function — additive contribution to `compute_final_score()`.

| Category | Trigger | Weight |
|---|---|---|
| Windows/Teams stack | `microsoft teams`, `ms teams`, `windows environment`, `microsoft 365`, `o365` | −2.0 |
| Mac/Slack stack | `slack`, `macbook`, `macos`, `mac environment`, `google workspace`, `g suite` | +1.0 |
| 2nd-time founder | research flag | +1.0 |
| 1st-time founder | research flag | −1.0 |
| Greenfield bonus | `build from scratch`, `greenfield`, `first hire`, `founding team`, `0 to 1`, `create the function`, `build the team`, `define the strategy`, `stand up`, `architect` | +2.0 |
| Process upcycling | `clean up`, `fix broken`, `optimize existing`, `improve current`, `streamline`, `reduce manual`, `automate existing` (only if greenfield NOT detected) | −1.0 |
| Consumption pricing | `consumption`, `usage-based`, `pay-as-you-go`, `metered`, `token-based`, `credit-based`, `outcome-based` | +1.0 |
| AI-native sector | sector contains `ai`, `ai-native`, `machine learning` | +2.0 |
| DevTools sector | sector contains `developer tools`, `devtools`, `dev ops` | +1.5 |
| Fintech sector | sector contains `fintech`, `finance` | +1.0 |
| Fully remote | `100% remote`, `fully remote`, `remote-first`, `work from anywhere` | +0.5 |
| CS shrinking + Sales growing | research flag (`cs_shrinking_sales_growing`) | −1.0 |
| CFO/CRO antagonism | research flag — display only | 0.0 |
| Runway < 18 months | research flag — display only | 0.0 |

---

## 6. Final composition

```python
raw = base (profile["scoring"]["base_score"], default 5.0)
    + adjustment_weights_score   # Layer 4 (can be negative)
    + match_score                # Layer 2
    + llm_adjustment             # Layer 3
final = round(max(0.0, min(10.0, raw)), 1)
```

**Single end-clamp.** The previous engine clamped Layer 4 to [0, 10] before adding
the LLM nudge, which made max-deterministic roles insensitive to the LLM. That
bug is fixed in `compute_final_score()`.

Score tiers (`engine.py::SCORE_TIERS`):

| Score | Tier | Color |
|---|---|---|
| ≥ 9.0 | Dream Job | gold |
| ≥ 7.0 | Solid Bet | green |
| ≥ 5.0 | Worth a Look | yellow |
| ≥ 3.0 | Probably Skip | orange |
| > 0.0 | Hard Pass | red |
| 0.0 | Auto-Rejected | (same red, label differs) |

---

## 7. Persistence

`app/sheets/sync.py::save_job_to_db()` writes the score record to SQLite. New columns
on the `jobs` table (added via additive `ALTER TABLE ... ADD COLUMN` in `models.py`):

- `match_score REAL`
- `match_summary TEXT`
- `match_evidence_json TEXT`
- `match_mismatches_json TEXT`
- `match_bullets_json TEXT`
- `match_hooks_json TEXT`
- `differentiator_themes_json TEXT`
- `adjustment_weights_score REAL`

`score_history` mirrors `match_score` and `adjustment_weights_score` so trends are
preserved over time.

---

## 8. UI surfaces

- `/settings/methodology` — full per-layer documentation (must stay in sync with this spec).
- `/job/{id}` — job detail page. **Match Analysis** tab renders:
  - Match score and summary
  - Evidence table (JD requirement → matched accomplishment → strength)
  - Mismatches list (severity-coded)
  - Differentiator themes (chips)
  - Tailored resume bullets
  - Cover-letter hooks
- The **Score breakdown** tab on the job detail page shows all four layer contributions stacked.

---

## 9. Cost / performance

- Two LLM calls per scored job (Layers 2 and 3).
- On `gemini-2.5-flash` at Tier 1: ~$0.005–$0.02 per job, sub-3s latency.
- If Layer 2's prompt exceeds ~20k input tokens, trim the corpus payload (drop
  tags-only bullets, keep headlines + metrics). `render_corpus_for_prompt()` in
  `corpus.py` already caps output at 18k chars.

---

## 10. Failure modes

| Scenario | Behavior |
|---|---|
| Inventory docx missing | Layer 2 returns `match_score=0.0` + High-severity mismatch entry pointing at the missing file. Layers 3 and 4 still run. |
| Layer 2 LLM call raises | Same as missing docx — zero contribution, mismatch entry naming the exception. |
| Layer 3 LLM call raises | Bubbles up. Job is not saved. (Existing behavior, unchanged.) |
| Auto-reject triggered | `final=0.0`. Adjustment-weights flags still computed and stored for forensics. Layer 2 and Layer 3 are skipped. |
