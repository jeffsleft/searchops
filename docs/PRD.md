# Product Requirements Document: Recruiting Engine (Reverse ATS)

**Version:** 1.0
**Author:** Jeff Beaumont (via Claude, synthesized from Gemini research sessions)
**Date:** 2026-05-07
**Status:** Ready for build

---

## 1. Problem Statement

**Situation:** Jeff Beaumont is transitioning from Finance Director at Mercy Ships
to a VP/Director-level GTM Operations role at a 200-500 person SaaS company. He
returns to the US in June/July 2026 with a target start date of Sept-Oct 2026.

**Complication:** The job search process is high-drag and low-signal. LinkedIn
surfaces hundreds of roles with no way to filter against nuanced preferences
(tech stack, pricing model, strategic debt, founder type). Companies use ATS to
score candidates — candidates have no equivalent system to score companies.
Interview preparation is ad-hoc, question tracking is manual, and there's no
systematic way to accumulate intelligence across multiple interviews with the
same company.

**Resolution:** Build a "Reverse ATS" — a personal job search engine that scores
companies against the candidate's profile, tracks the full interview funnel, and
provides structured interview preparation tools. Deploy as a private web app on
Modal with Google Sheets as the human-editable data layer.

---

## 2. Users

**Primary user:** Jeff Beaumont (single-user app, password-protected).

**Future users (Phase 2):** Other job seekers who can fork the repo, plug in
their own `candidate_profile.yaml`, and run their own instance.

---

## 3. System Components

### 3.1 Reverse ATS Scoring Engine

The core differentiator. Takes a job listing (URL or pasted text) and returns
a structured score with explanation.

**Input methods:**
- Drop a URL into Google Sheets "To Evaluate" tab → Watchdog picks it up
- Paste a URL or JD text into the web UI → Immediate scoring
- Bulk import from CSV (for Crunchbase-style lists)

**Scoring has two layers:**

#### Layer 1: Deterministic Rules (runs first, fast, no LLM cost)

These are hard-coded from `candidate_profile.yaml`:

| Rule                     | Action        | Weight  |
|--------------------------|---------------|---------|
| Base salary < $120k      | Auto-reject   | —       |
| Sector in blocklist      | Auto-reject   | —       |
| Ethics flag (gambling,   | Auto-reject   | —       |
| adult, predatory lending)|               |         |
| Windows / MS Teams env   | Penalty       | -2.0    |
| Mac / Slack / Google env | Bonus         | +1.0    |
| 2nd-time founder         | Bonus         | +1.0    |
| 1st-time founder         | Penalty       | -1.0    |
| Greenfield / Blue Ocean  | Bonus         | +2.0    |
| Process upcycling only   | Penalty       | -1.0    |
| Consumption/outcome      | Bonus         | +1.0    |
| pricing model            |               |         |
| Target sector match      | Bonus         | +1.5    |
| (DevTools, AI SaaS,      |               |         |
| Fintech, PropTech,       |               |         |
| Construction, Bev/Log)   |               |         |
| 100% remote              | Bonus         | +0.5    |
| CFO/CRO antagonism       | Warning flag  | —       |
| CS shrinking / Sales     | Penalty       | -1.0    |
| growing (churn & burn)   |               |         |

Base score starts at 5.0. Adjustments applied. Floor: 0. Ceiling: 10.

**Important:** If a job is auto-rejected by the deterministic layer, the LLM
layer is skipped entirely. This saves token cost on obvious non-starters.

#### Layer 2: LLM Qualitative Analysis (runs if not auto-rejected)

The LLM receives:
- The job description text
- The candidate profile summary
- The deterministic score and flags

It returns:
- Qualitative score adjustment (-1.0 to +1.0)
- 4-5 line pros/cons summary
- Greenfield assessment (Yes / No / Partial) with 1-sentence rationale
- Pricing model classification (Seat / Consumption / Outcome / Hybrid / Unknown)
- Recommended interview angle (1 sentence)

**Final score = deterministic_score + llm_adjustment**, clamped to [0, 10].

#### Output per job:

```json
{
  "company": "string",
  "job_title": "string",
  "url": "string",
  "date_found": "2026-05-07",
  "final_score": 8.2,
  "deterministic_score": 7.5,
  "llm_adjustment": 0.7,
  "auto_rejected": false,
  "reject_reason": null,
  "pros": "string (4-5 lines)",
  "cons": "string (4-5 lines)",
  "greenfield": "Yes|No|Partial",
  "greenfield_rationale": "string",
  "pricing_model": "Consumption|Seat|Outcome|Hybrid|Unknown",
  "sector": "string",
  "recommended_angle": "string",
  "tech_stack_detected": {
    "crm": "Salesforce|HubSpot|Unknown",
    "cs_tool": "Gainsight|Vitally|Unknown",
    "comms": "Slack|Teams|Unknown",
    "os": "Mac|Windows|Unknown",
    "cloud": "AWS|GCP|Azure|Unknown"
  },
  "flags": ["greenfield", "windows_penalty", "target_sector_match"]
}
```

### 3.2 Company Research Agent

After initial scoring, a research agent runs deeper due diligence.

**Trigger conditions:**
- Automatic: job scores 6.0 or higher after scoring
- Manual: Jeff clicks "Research" in the UI for any job
- Never: auto-rejected jobs (score 0) do not trigger research

**Research produces:**

| Field               | Source Approach                        |
|---------------------|---------------------------------------|
| Funding stage       | LLM web search or manual entry        |
| Total raised        | LLM web search or manual entry        |
| Headcount           | LLM web search or manual entry        |
| Headcount trend     | LLM web search (growing/flat/shrinking)|
| CS team trend       | LLM web search (vs Sales growth)      |
| Revenue model       | PLG / SLG / Hybrid                    |
| Pricing model       | Seat / Consumption / Outcome / Hybrid |
| HQ location         | LLM web search                        |
| CEO founder type    | 1st-time / 2nd-time / Professional CEO|
| Tech stack          | LLM web search + JD signals           |
| Competitors         | LLM web search (top 3)                |
| Red flags           | LLM assessment                        |
| 1st-degree contacts | Manual entry (LinkedIn check by Jeff) |
| Estimated runway    | If data available, flag <18 months    |
| Has FDE model       | Yes / No / Unknown                    |
| Outreach hook       | 1-sentence personalized intro         |

### 3.3 Interview Pipeline Tracker

A Kanban-style funnel tracker for all opportunities.

**Stages:**

| Stage           | Description                                  |
|-----------------|----------------------------------------------|
| Identified      | Found the listing, not yet evaluated         |
| Evaluated       | Scored by the engine, awaiting Jeff's review |
| Researching     | Deep research underway                       |
| Outreach        | Jeff is reaching out or applying             |
| Recruiter Screen| First call with recruiter/HR                 |
| HM Interview    | Hiring manager conversation                  |
| Panel / Loop    | Multi-person interview round                 |
| Final / Offer   | Final stage or offer in hand                 |
| Accepted        | Offer accepted                               |
| I Declined      | Jeff chose not to proceed (with notes)       |
| They Declined   | Company declined Jeff (with notes)           |
| On Hold         | Paused for any reason                        |

**Each pipeline entry tracks:**
- Company name, role title, URL
- Current stage + stage history with timestamps
- All contacts (name, title, LinkedIn URL, persona type)
- Notes per stage
- Strategy brief (accumulates across interviews)
- Attached files (resume version sent, prep notes)
- Ethics vetted (boolean, manual)
- Decline reason (if applicable, which party, why)

### 3.4 Question Bank

A categorized, prioritized bank of interview questions. Questions are:
- **Seeded** from a master template at pipeline entry
- **Generated** by the LLM based on company research and JD
- **Accumulated** from transcript analysis (unanswered questions roll forward)
- **Manually added** by Jeff

**Question schema:**

| Field          | Type                              |
|----------------|-----------------------------------|
| question       | Text                              |
| category       | Financial / Strategic / Technical / Cultural / Pricing / Operational |
| persona_target | CFO / CRO / COO / CCO / VP Eng / Founder / Recruiter / Any |
| priority       | High / Medium / Low               |
| status         | Unasked / Asked / Answered / Deferred |
| company        | FK to pipeline entry              |
| source         | Seed / Research / Transcript / Manual |
| answer_notes   | Text (filled after interview)     |
| asked_to       | Contact name (filled after)       |
| divergence_flag| Boolean (same Q asked to multiple people with different answers) |

**Divergence detection:** When the same question is asked to two different people
(e.g., CFO and CRO), and both have answer notes, the system prompts the LLM to
assess whether the answers align or diverge. Divergence is flagged with a brief
explanation. This helps Jeff detect collaborative vs. antagonistic environments.

### 3.5 Strategy Brief

A per-company living document that accumulates intelligence across all
interactions. Not rewritten — appended.

**Sections:**

1. **Company Overview** — auto-populated from research agent
2. **Role Analysis** — from JD scoring
3. **Operational Debt Assessment** — process debt vs. strategic debt (from
   research + interview signals)
4. **Metrics & Pricing Model** — what metrics they use for Sales, Finance, CS.
   Whether they have FDE. Where FDE sits organizationally.
5. **Competitive Landscape** — top 3 competitors, how they're positioned
6. **Interview Intelligence** — appended after each conversation:
   - Key signals detected
   - Unanswered questions
   - Interviewer persona assessment
   - Divergence notes
7. **Jeff's Performance Scorecard** — self-assessment after each interview:
   - What went well
   - What to improve
   - Which anchor stories landed
   - Confidence level (1-5)
8. **Recommendation** — LLM-generated after 2+ interviews: Should Jeff proceed?
   What's the risk? What's the upside?

### 3.6 Transcript Analysis

When Jeff provides an interview transcript (from Granola, Otter, or manual),
the system analyzes it and produces:

- **Unanswered questions** → added to question bank as High priority
- **Signals of operational debt** → categorized as process or strategic debt
- **Interviewer persona classification** (e.g., "The Technical Skeptic,"
  "The Visionary Founder," "The Financial Operator")
- **Jeff's performance notes** — what he said well, where he was vague
- **Strategy brief update** — new intelligence appended

### 3.7 Mock Interview Prep

A prompt-driven tool that generates:
- Top 10 likely questions for the next interview round
- Recommended anchor stories matched to each question
- A "cheat sheet" one-pager for quick review

Jeff's anchor stories (from his profile):
1. GitLab NRR exceeding 130% — health data, insights, sharing
2. Customer health scoring build and iteration
3. Digital customer success at scale
4. Supporting professional services / FDE model
5. Mercy Ships finance ops in high-constraint environment
6. Leadership development and team building

### 3.8 Notifications (Slack)

- Score > 8.0: Slack alert with company, score, pros/cons, greenfield flag
- Pipeline stage change: brief update
- Weekly digest: summary of pipeline state (how many at each stage)

---

## 4. Data Model

### 4.1 Google Sheets (Human Interface)

**Tab: "To Evaluate"**

| Column | Field            | Who writes | Notes                      |
|--------|------------------|------------|----------------------------|
| A      | Date Added       | App/Jeff   | ISO 8601                   |
| B      | Company          | App        | Extracted from JD          |
| C      | Job Title        | App        | Extracted from JD          |
| D      | Job URL          | Jeff       | The input — Jeff drops URLs|
| E      | Status           | App        | Identified/Scored/Research Complete |
| F      | Final Score      | App        |                            |
| G      | Pros             | App        |                            |
| H      | Cons             | App        |                            |
| I      | Greenfield       | App        | Yes/No/Partial             |
| J      | Pricing Model    | App        |                            |
| K      | Sector           | App        |                            |
| L      | Funding Stage    | App        |                            |
| M      | Total Raised     | App        |                            |
| N      | Headcount        | App        |                            |
| O      | HQ               | App        |                            |
| P      | CEO Type         | App        | 1st/2nd/Professional       |
| Q      | Tech Stack       | App        | Brief summary              |
| R      | Competitors      | App        |                            |
| S      | Red Flags        | App        |                            |
| T      | Outreach Hook    | App        |                            |
| U      | Jeff's Notes     | Jeff       | Manual column              |
| V      | Pipeline Stage   | Jeff/App   | Synced with app pipeline   |
| W      | Ethics Vetted    | Jeff       | Manual checkbox            |

**Tab: "Interview Bank"** (read by app for seeding questions)

| Column | Field            |
|--------|------------------|
| A      | Company          |
| B      | Persona Target   |
| C      | Question         |
| D      | Priority (H/M/L) |
| E      | Category         |
| F      | Status           |
| G      | Answer Notes     |

**Tab: "Crunchbase Import"** (optional — bulk company data)

Jeff may paste Crunchbase export data here. The app reads company names and
cross-references against the "To Evaluate" tab.

### 4.2 SQLite (App State)

Tables:

- `jobs` — mirror of Sheets "To Evaluate" + full scoring JSON
- `pipeline` — stage history per job (timestamp, from_stage, to_stage, notes)
- `contacts` — people at each company (name, title, linkedin, persona_type)
- `questions` — full question bank per company
- `strategy_briefs` — per-company accumulating document (markdown)
- `transcripts` — raw transcript text + analysis results
- `interviews` — per-interview record (date, contacts, scorecard)
- `research_cache` — cached LLM research results (TTL: 7 days)
- `score_history` — historical scores for trend tracking

---

## 5. Frontend Specification (HTMX)

### 5.1 Pages

**Dashboard (`/`)**
- Summary stats: total jobs evaluated, pipeline distribution, avg score
- Recent high-scores (>7.0) with score cards
- Pipeline funnel visualization (horizontal bar or simple counts per stage)
- Quick-add: paste a URL, get scored immediately

**Job Detail (`/job/<id>`)**
- Full score breakdown (deterministic + LLM)
- Research results
- Pipeline stage with change controls
- Strategy brief (rendered markdown)
- Question bank for this company
- Contact list
- Interview history

**Pipeline (`/pipeline`)**
- Kanban-style view of all jobs by stage
- Drag-to-reorder or click-to-advance
- Filter by score range, sector, greenfield flag

**Interview Prep (`/prep/<job_id>`)**
- Upcoming interview details (who, when, what round)
- Question bank filtered to this company, sorted by priority
- Suggested anchor stories
- Cheat sheet generator (one-pager)
- Transcript upload/paste + analysis trigger

**Settings (`/settings`)**
- View/edit candidate profile (read-only display of YAML)
- Google Sheets connection status
- Slack webhook test
- LLM provider toggle
- Manual sheet sync trigger

### 5.2 UI Patterns

- **Score display:** Color-coded badge. Red (0-4), Yellow (5-6), Green (7-8),
  Blue/Gold (9-10).
- **Greenfield flag:** Distinct icon/badge. Yes=green sprout, Partial=yellow,
  No=gray.
- **Stage badges:** Color per stage. Active stages are vivid; terminal stages
  (declined, accepted) are muted.
- **HTMX patterns:** Use `hx-get` for navigation, `hx-post` for mutations,
  `hx-trigger="load"` for lazy-loading expensive sections (research, strategy
  brief). Use `hx-swap="innerHTML"` for partial updates.

---

## 6. API Endpoints

All endpoints are Modal `@modal.web_endpoint` or `@modal.asgi_app` functions.

| Method | Path                      | Description                     |
|--------|---------------------------|---------------------------------|
| GET    | `/`                       | Dashboard                       |
| GET    | `/login`                  | Login form                      |
| POST   | `/login`                  | Authenticate                    |
| GET    | `/job/<id>`               | Job detail page                 |
| POST   | `/job/score`              | Score a new URL or pasted JD    |
| POST   | `/job/<id>/research`      | Trigger deep research           |
| POST   | `/job/<id>/stage`         | Update pipeline stage           |
| GET    | `/pipeline`               | Pipeline view                   |
| GET    | `/prep/<job_id>`          | Interview prep page             |
| POST   | `/prep/<job_id>/question` | Add question to bank            |
| POST   | `/prep/<job_id>/transcript` | Upload/paste transcript       |
| POST   | `/prep/<job_id>/cheatsheet` | Generate cheat sheet          |
| GET    | `/settings`               | Settings page                   |
| POST   | `/sync`                   | Manual Google Sheets sync       |
| POST   | `/api/webhook/score`      | Programmatic scoring endpoint   |

---

## 7. Scheduled Functions (Modal)

| Function     | Schedule        | Description                          |
|-------------- |-----------------|--------------------------------------|
| `watchdog`   | Every 60 min    | Check Sheets for new URLs, score them|
| `weekly_digest`| Monday 8am PT | Slack summary of pipeline state      |

---

## 8. LLM Prompt Architecture

All prompts live in `app/scoring/prompts.py` as named constants.

Key prompts:

1. **SCORING_PROMPT** — Analyze JD against candidate profile, return structured score
2. **RESEARCH_PROMPT** — Deep company research with web search
3. **TRANSCRIPT_ANALYSIS_PROMPT** — Extract signals, unanswered Qs, persona type
4. **DIVERGENCE_PROMPT** — Compare two answer notes for the same question
5. **CHEATSHEET_PROMPT** — Generate interview prep one-pager
6. **STRATEGY_BRIEF_UPDATE_PROMPT** — Append new intelligence to existing brief
7. **MOCK_QUESTIONS_PROMPT** — Generate likely questions + anchor story matches

Each prompt includes the candidate profile summary as context. The profile
summary is generated once from `candidate_profile.yaml` and cached.

---

## 9. Security

- Password auth via session cookie. Single password set in Modal Secrets.
- No PII stored beyond what Jeff manually enters about contacts.
- Google Sheets access via OAuth token (already set up).
- All LLM calls use API keys stored in Modal Secrets.
- Credentials files (`credentials.json`, `token.json`) must NOT be in git.
  Add to `.gitignore` immediately.

---

## 9.5 Phase 0: Automated Job Discovery (NEW)

Before jobs enter the scoring pipeline, an automated discovery layer surfaces
matching roles at monitored target companies.

**Core idea:** Instead of waiting for Jeff to manually find roles and drop URLs,
the system proactively scans company ATS portals (Greenhouse, Lever, Ashby) on a
daily schedule and surfaces roles that match keyword filters.

**Input:** List of companies to monitor, with their careers page URL.

**Process:**
1. Daily at 6am UTC, the scheduler triggers `run_discovery_scan()`
2. For each hunt-enabled company:
   - Fetch all open jobs from their ATS API (no auth required)
   - Quick keyword filter (skip titles without ops/gtm/revops/strategy)
   - For matching titles, generate LLM-powered fit analysis (3-4 specific bullets + preliminary score)
   - Insert into jobs table with `pipeline_stage='discovered'`
3. Send Slack notification if new jobs found

**Output:** New "Discovered" stage in pipeline. Jobs live here until manually promoted to "Identified" (and scored) or dismissed.

**Key benefits:**
- No more manual job search on LinkedIn / Crunchbase
- Real-time alerts via Slack when matching roles appear
- Specific fit signals (from LLM) help prioritize which to pursue
- Easy to add/remove target companies via /targets UI

**Configuration:** Hunt targets are managed in `/targets` — each company has:
- ATS type (auto-detected from careers URL)
- Last scanned timestamp
- Error log (if fetch failed)

**Routes:**
- GET /discovered — view all discovered jobs
- POST /job/{id}/promote — move to identified stage, trigger scoring
- POST /job/{id}/dismiss — mark as declined
- GET /targets — manage monitored companies
- POST /targets/add — add new target company
- POST /targets/{id}/scan-now — manual immediate scan

**Specifications:** See `docs/discovery-engine-spec.md`.

---

## 10. Out of Scope for v1

- Multi-user support
- Auto-scraping LinkedIn (anti-bot risk)
- Calendar integration
- Email drafting/sending
- Mobile-optimized UI (desktop-first)
- Crunchbase API integration (manual CSV import only)
- Automated outreach
- Resume tailoring per application
- Playwright fallback for unmapped ATS systems (Phase 1+)

---

## 11. Success Criteria

The app is "done" for v1 when:
1. Jeff can drop a LinkedIn job URL into Google Sheets and get a scored
   result within 60 minutes (via watchdog) or immediately (via UI).
2. Each scored job has a deterministic + LLM score, pros/cons, and
   greenfield flag.
3. Jeff can move jobs through pipeline stages in the UI.
4. Jeff can add questions, view them by persona/priority, and mark them
   as asked/answered.
5. Jeff can paste a transcript and get analysis + question bank updates.
6. Strategy briefs accumulate across interviews.
7. Slack notifications fire for scores > 8.0.
8. The app is deployed on Modal and accessible via password-protected URL.
