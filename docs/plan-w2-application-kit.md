# W2 — Application Kit (PR description draft)

Status: **awaiting Jeff's nod on layout.** Do not start building until that lands.
Spec parent: `docs/plan-dual-track-2026-07-14.md` → Wave 2. Todoist: W2 Application Kit (p2, due 2026-07-31).

## What this actually is

One click from a gate-cleared job → a ready-to-send package. **Every component already
exists and already renders** on `job_detail.html`. This wave is assembly + a gate + one
export path — not construction. Scope is smaller than the parent plan implies for Track 1,
and larger for Track 2 (see "The forkability gap").

Verified present before writing this spec:

| Component | Where it already lives |
|---|---|
| Recommended angle | `job_detail.html:189` (`jobs.recommended_angle`) |
| Evidence table (L2) | `job_detail.html:382` (`match_evidence_json`) |
| Mismatches + severity | `job_detail.html:415` (`match_mismatches_json`) |
| Tailored resume bullets | `job_detail.html:445` (`match_bullets_json`) |
| Cover-letter hooks | `job_detail.html:466` (`match_hooks_json`) |
| Cover letter + voice pass | `routes.py:588` → `app/voice.polish_cover_letter` |
| Resume `.docx` | `routes.py:513` `build_resume_docx` |
| Cover letter `.docx` | `routes.py:657` `build_cover_letter_docx` |

## The forkability gap (the real work)

The parent plan lists "works off the example profile" as satisfied. It is not, and nothing
in the repo makes it so:

- `data/*.docx` is gitignored (`.gitignore:57`); only `data/README.md` is tracked. **A fresh
  clone has no `Accomplishments_Inventory.docx`.**
- `corpus.py:176` then returns `available: False` → Layer 2 contributes 0.0.
- `seed_data/example_scores.json` carries **no** `match_*` fields — only Layer 3/4 output
  (`recommended_angle`, `pros`, `cons`, `greenfield`).

Evidence, bullets, and hooks are *all* Layer 2 outputs. So on a fresh clone the kit's three
headline sections render empty, with no demo data to fall back on. This is the same failure
class `memory/lessons_learned.md:91` already caught — the demo dataset contradicting the
product's own claims.

**Decision (Jeff, 2026-07-16): ship a real example inventory.** Script-generate a fictional
`Accomplishments_Inventory` for the existing persona and let Layer 2 genuinely run on a
fresh clone. Rejected: pre-baking canned `match_*` into seed data (stages output the
forker's own clone can't reproduce); degrading honestly (stranger never sees the flagship
work); deferring (leaves the stated acceptance bar unmet).

## Build order

### 1. Example corpus — unblocks everything else
- New `scripts/build_example_corpus.py`, same shape as `scripts/build_doc_templates.py`
  (python-docx → committed artifact).
- Output `data/Accomplishments_Inventory.example.docx`; add a `.gitignore` negation so this
  one file commits while Jeff's real inventory stays ignored.
- Persona is **already fixed** — `candidate_profile.example.yaml`: Alex Rivera, Head of
  RevOps at AcmeSaaS, $30M→$150M ARR, CPA + RevOps, seat→consumption pricing, NRR 95%→112%.
  Corpus must match those differentiators exactly, or the demo contradicts its own profile.
- Structure per `data/README.md`: H1 company/role, H2 theme, bullets with `#hashtag` tags,
  top-level `Differentiators` H1.
- `corpus.py` falls back to the example file when the real inventory is absent. Surface
  which corpus loaded in the kit header — never let a forker mistake demo evidence for real.
- `INVENTORY_EXPECTED_SHA256` (`corpus.py:35`) is pinned to Jeff's file, so a forker's own
  inventory warns on every load. Make the constant tolerate the example + a forker's file.

**Quality bar:** thin filler makes the demo worse than no demo. The corpus needs real
texture — specific numbers, real-sounding systems — or Track 2's flagship reads as a stub.

### 2. Voice pass → service layer (prerequisite, not cleanup)
`polish_cover_letter` is called from `routes.py:598`, *not* from `cover_letter.py` or
`scoring_service.py`. The kit routes through the service layer per W3-A direction — done
naively it **silently skips the voice pass**, while the task explicitly requires
"voice-passed cover letter". Move the polish into the generation path so the kit and the
existing route share one code path. Regression check: existing CL route still polishes.

### 3. Kit service + view
- `app/services/kit_service.py` — assembles the kit dict. No logic in `routes.py` (W3-A).
- `app/templates/kit.html` + route `/job/{id}/kit`.
- Gate: `final_score > top_band_gate_threshold` (7.0, `candidate_profile.yaml:6`) **and**
  `ethics_vetted` manually confirmed. Ethics stays manual — charter constraint. Gate failure
  renders *why*, with the ethics toggle inline; it never 404s.
- Layout: gate/status header → recommended angle → evidence → mismatches → bullets +
  cover letter → export. Per approved mockup.

### 4. Combined export
- `build_kit_docx` — resume + cover letter + one-page brief (angle, evidence, mismatches),
  reusing the existing `docxtpl` masters in `app/templates/docx/`.
- Single download. The two existing per-artifact downloads stay.

## Acceptance

- Gate-cleared job → complete kit in **<2 min** → review-and-send.
- Fresh clone + `candidate_profile.example.yaml` + **no** real inventory → kit renders with
  real Layer 2 evidence off the example corpus, labelled as example data.
- Voice pass demonstrably runs on the kit's cover letter (test asserts it, not eyeballed).
- Existing resume/CL routes unregressed.
- Tests: extend `tests/test_doc_render.py` (kit docx), `tests/test_voice.py` (kit path).

## Out of scope

- Scoring-engine changes (parent plan: frozen until ≥25 calibration outcomes).
- The W3-A/W3-B refactors beyond the voice-pass move above.
- Public case-study kit section → W3-D, after a redaction check.
