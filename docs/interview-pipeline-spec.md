# Interview Pipeline & Question Bank Specification

## Overview

This spec covers everything after the scoring engine: how jobs move through
the interview pipeline, how questions are managed, how strategy briefs accumulate,
and how transcripts are analyzed.

---

## 1. Pipeline Stages

### Stage Definitions

| Stage            | Code             | Terminal? | Description                            |
|------------------|------------------|-----------|----------------------------------------|
| Identified       | `identified`     | No        | Found the listing. Not yet scored.     |
| Evaluated        | `evaluated`      | No        | Scored by engine. Awaiting Jeff review.|
| Researching      | `researching`    | No        | Deep research in progress.             |
| Outreach         | `outreach`       | No        | Jeff is applying or reaching out.      |
| Recruiter Screen | `recruiter`      | No        | First call with recruiter/HR.          |
| HM Interview     | `hm_interview`   | No        | Hiring manager conversation.           |
| Panel / Loop     | `panel`          | No        | Multi-person interview round.          |
| Final / Offer    | `final_offer`    | No        | Final stage or offer received.         |
| Accepted         | `accepted`       | Yes       | Offer accepted.                        |
| I Declined       | `i_declined`     | Yes       | Jeff chose not to proceed.             |
| They Declined    | `they_declined`  | Yes       | Company declined Jeff.                 |
| On Hold          | `on_hold`        | No        | Paused for any reason.                 |

### Stage Transitions

Allowed transitions (enforced in app):

```
identified → evaluated → researching → outreach → recruiter → hm_interview → panel → final_offer → accepted
                                                                                                  → i_declined
                                                                                                  → they_declined

# Any non-terminal stage can move to:
# - on_hold (and back to previous stage)
# - i_declined
# - they_declined

# Backward movement is allowed (e.g., panel → hm_interview for another round)
```

### Stage History

Every stage change is recorded:

```sql
CREATE TABLE pipeline_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    from_stage TEXT,
    to_stage TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    changed_by TEXT DEFAULT 'jeff'  -- For future multi-user
);
```

### Decline Tracking

When a job moves to `i_declined` or `they_declined`, a reason is required:

**I Declined reasons:**
- Compensation too low
- Not enough greenfield
- Bad culture signals
- Wrong tech stack
- Too much travel
- Ethics concern
- Better opportunity elsewhere
- Role not senior enough
- Other (free text)

**They Declined reasons:**
- Not enough experience
- Overqualified
- Location mismatch
- Compensation mismatch
- Went with another candidate
- Role cancelled / hiring freeze
- No response (ghosted)
- Other (free text)

---

## 2. Contact Management

### Contact Schema

```sql
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    name TEXT NOT NULL,
    title TEXT,
    linkedin_url TEXT,
    email TEXT,
    persona_type TEXT,  -- From persona_types list
    relationship TEXT,  -- '1st_degree', '2nd_degree', 'cold'
    notes TEXT,
    met_on DATE,  -- Date of first interaction
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Persona Types

**Important: There are two distinct persona systems. Do not conflate them.**

**1. Interviewer Archetypes** (assigned to contacts after interactions):
- The Technical Skeptic
- The Visionary Founder
- The Financial Operator
- The Growth Evangelist
- The Process Fixer
- The Culture Guardian

These are stored on the `contacts` table in `persona_type`. Assigned manually
by Jeff or suggested by transcript analysis.

**2. Role Personas** (used on questions to specify who to ask):
- CFO, CRO, COO, CCO, VP Eng, Founder, Recruiter, Any

These are stored on the `questions` table in `persona_target`. Used to route
questions to the right person during interview prep.

The two systems serve different purposes: archetypes describe *how* someone
thinks; role personas describe *what role* they hold. A CRO could be a
"Visionary Founder" archetype or a "Growth Evangelist" archetype.

---

## 3. Question Bank

### Schema

```sql
CREATE TABLE questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    question TEXT NOT NULL,
    category TEXT NOT NULL,       -- Financial, Strategic, Technical, Cultural, Pricing, Operational
    persona_target TEXT,          -- CFO, CRO, COO, CCO, VP Eng, Founder, Recruiter, Any
    priority TEXT NOT NULL,       -- High, Medium, Low
    status TEXT DEFAULT 'unasked', -- unasked, asked, answered, deferred
    source TEXT NOT NULL,         -- seed, research, transcript, manual
    answer_notes TEXT,
    asked_to TEXT,                -- Contact name
    asked_on DATE,
    divergence_flag BOOLEAN DEFAULT FALSE,
    divergence_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Seed Questions

When a job enters the pipeline (moves to `researching` or later), a set of
seed questions is auto-generated. These come from two sources:

**1. Universal seed questions** (apply to every company):

```yaml
seed_questions:
  high_priority:
    - category: Financial
      persona: CFO
      question: "What is your current runway, and what does the path to profitability look like?"
    - category: Financial
      persona: CRO
      question: "What is your current NRR, and how has it trended over the last 4 quarters?"
    - category: Strategic
      persona: Founder
      question: "What is the #1 piece of strategic debt you are currently 'powering through' with manual effort?"
    - category: Strategic
      persona: CRO
      question: "How would you describe the ICP today vs. 12 months ago? Has it shifted?"
    - category: Pricing
      persona: Any
      question: "What is your pricing model? Seat-based, consumption, outcome-based, or a hybrid?"
    - category: Pricing
      persona: CFO
      question: "Are you transitioning pricing models? If so, what stage is the transition?"
    - category: Operational
      persona: Any
      question: "Do you have a Forward Deployed Engineering (FDE) team? Where does it sit organizationally?"
    - category: Cultural
      persona: Any
      question: "Are we using AI to make our current messy process faster, or are we building new capabilities that weren't possible before?"
    - category: Technical
      persona: Any
      question: "What is your current tech stack? CRM, CS platform, communication tools?"

  medium_priority:
    - category: Strategic
      persona: CRO
      question: "When a customer is technically successful but expensive to serve, how do you decide between gross margin and market share?"
    - category: Cultural
      persona: Any
      question: "How would you describe the relationship between Sales and CS today?"
    - category: Operational
      persona: Any
      question: "What metrics does the CS team own today? How are those changing?"
    - category: Financial
      persona: CFO
      question: "What does your customer segmentation look like from a revenue perspective?"
    - category: Technical
      persona: VP Eng
      question: "What does your data infrastructure look like for customer usage signals?"

  low_priority:
    - category: Financial
      persona: CFO
      question: "How are your bank accounts and treasury operations structured?"
    - category: Cultural
      persona: Any
      question: "What does the onboarding process look like for a new leader at this level?"
    - category: Operational
      persona: Any
      question: "How large is the team I would be managing, and what does the org chart look like?"
```

**2. Research-generated questions** (generated by LLM after company research):

The research agent identifies gaps in available information and generates
targeted questions. These are added with `source: research` and priority
based on information importance.

### Question Lifecycle

```
Created (seed/research/transcript/manual)
    ↓
Unasked → Jeff reviews, adjusts priority
    ↓
Asked → Jeff marks after asking in interview, adds `asked_to` and `asked_on`
    ↓
Answered → Jeff adds answer notes
    ↓
[Optional] Divergence check → If same Q asked to 2+ people
```

### Divergence Detection

When the same question (or semantically similar) has `answered` status with
two different `asked_to` contacts:

1. System surfaces both answer notes
2. LLM compares them using the divergence prompt:

```
DIVERGENCE_PROMPT = """
The same question was asked to two different leaders at {company}.

Question: {question}

Person A ({person_a_title}):
{answer_a}

Person B ({person_b_title}):
{answer_b}

Assess:
1. Do they fundamentally agree or diverge on this topic?
2. If divergent, what does this suggest about the organization?
3. Is this a red flag (antagonistic relationship) or healthy tension?

Return JSON:
{{
  "aligned": <true|false>,
  "summary": "<2-3 sentences explaining the alignment or divergence>",
  "red_flag": <true|false>,
  "red_flag_reason": "<1 sentence if red_flag is true, else null>"
}}
"""
```

3. Result is stored in `divergence_flag` and `divergence_notes` on the question.
4. Also surfaced in the Strategy Brief under "Interview Intelligence."

---

## 4. Strategy Brief

### Structure

Each company gets a living Strategy Brief stored as markdown in SQLite. It is
never rewritten — only appended. Sections:

```markdown
# Strategy Brief: {Company Name}

## Company Overview
<!-- Auto-populated from research agent -->
- Funding: {stage}, {raised}
- Headcount: {count} ({trend})
- Revenue Model: {PLG/SLG}
- Pricing: {model}
- HQ: {location}
- Competitors: {list}
- CEO: {name} ({founder_type})

## Role Analysis
<!-- From scoring engine -->
- Title: {title}
- Score: {score}/10
- Greenfield: {assessment}
- Recommended Angle: {angle}

## Operational Debt Assessment
<!-- Updated after research and interviews -->
### Process Debt
- {items}

### Strategic Debt
- {items}

## Metrics & Pricing Model
<!-- What metrics they use, FDE status -->
- Sales metrics: {known or Unknown}
- CS metrics: {known or Unknown}
- Finance metrics: {known or Unknown}
- Has FDE: {Yes/No/Unknown}
- FDE org position: {where it sits}

## Competitive Landscape
- {competitor_1}: {brief positioning}
- {competitor_2}: {brief positioning}
- {competitor_3}: {brief positioning}

## Interview Intelligence
<!-- Appended after each conversation -->

### {Date} — {Contact Name} ({Title})
- Key signals: {list}
- Unanswered questions: {list}
- Persona: {type}
- Divergence notes: {if applicable}

## Jeff's Performance Scorecard
<!-- Appended after each interview -->

### {Date} — {Round}
- What went well: {notes}
- What to improve: {notes}
- Anchor stories that landed: {list}
- Confidence level: {1-5}

## Recommendation
<!-- LLM-generated after 2+ interviews -->
{recommendation_text}
```

### Brief Update Flow

After each interview:
1. Jeff enters notes (what happened, key takeaways)
2. If transcript is available, transcript analysis runs
3. LLM generates a "Strategy Brief Update" using:

```
STRATEGY_BRIEF_UPDATE_PROMPT = """
You are updating a Strategy Brief for {company} after a new interview.

## Existing Brief
{current_brief}

## New Interview Data
Date: {date}
Contact: {contact_name} ({contact_title})
Round: {round}
Jeff's Notes: {jeff_notes}
Transcript Analysis: {transcript_analysis or "Not available"}

## Task
1. Add a new "Interview Intelligence" section entry for this conversation.
2. Update "Operational Debt Assessment" if new signals were detected.
3. Update "Metrics & Pricing Model" if new information surfaced.
4. If this is the 2nd+ interview, generate or update the "Recommendation" section.

Return the NEW SECTIONS ONLY as markdown. Do not repeat unchanged sections.
Format each new section with the appropriate heading level.
"""
```

---

## 5. Transcript Analysis

### Input
- Pasted text (from Granola, Otter, manual notes)
- Uploaded text file

### Analysis Prompt

```
TRANSCRIPT_ANALYSIS_PROMPT = """
Analyze this interview transcript for {candidate_name} interviewing at {company}
for the role of {job_title}.

## Transcript
{transcript_text}

## Candidate Profile (Summary)
{candidate_summary}

## Existing Questions for This Company
{existing_questions_json}

## Task
Return JSON:
{{
  "unanswered_questions": [
    {{
      "question": "<question that was raised but not answered>",
      "category": "<Financial|Strategic|Technical|Cultural|Pricing|Operational>",
      "priority": "<High|Medium|Low>",
      "context": "<why this question matters based on the transcript>"
    }}
  ],
  "operational_debt_signals": [
    {{
      "type": "<process|strategic>",
      "signal": "<what was said or implied>",
      "severity": "<High|Medium|Low>"
    }}
  ],
  "interviewer_persona": {{
    "name": "<contact name>",
    "persona_type": "<The Technical Skeptic|The Visionary Founder|etc.>",
    "evidence": "<1-2 sentences explaining the classification>"
  }},
  "jeff_performance": {{
    "strong_moments": ["<specific things Jeff said well>"],
    "weak_moments": ["<where Jeff was vague or missed an opportunity>"],
    "anchor_stories_used": ["<which anchor stories Jeff told>"]
  }},
  "key_signals": ["<important information learned about the company>"],
  "new_questions_to_ask": [
    {{
      "question": "<new question surfaced from this conversation>",
      "category": "<category>",
      "priority": "<priority>",
      "persona_target": "<who to ask>",
      "reason": "<why this question matters>"
    }}
  ]
}}
"""
```

### Post-Analysis Actions
1. Unanswered questions → added to question bank with `source: transcript`
2. New questions to ask → added to question bank with `source: transcript`
3. Operational debt signals → appended to strategy brief
4. Interviewer persona → saved to contact record
5. Jeff's performance → appended to strategy brief scorecard section
6. Key signals → appended to strategy brief interview intelligence

---

## 6. Mock Interview Prep / Cheat Sheet

### Cheat Sheet Generator

Triggered manually from the Interview Prep page. Produces a one-pager.

```
CHEATSHEET_PROMPT = """
Generate an interview preparation cheat sheet for {candidate_name} interviewing
at {company} for {job_title}.

## Company Research
{research_summary}

## Strategy Brief (Current)
{strategy_brief}

## Upcoming Interview
Round: {round}
Interviewer(s): {interviewers}
Date: {date}

## Candidate's Anchor Stories
{anchor_stories_json}

## Open Questions (High Priority)
{high_priority_questions}

## Task
Generate a concise cheat sheet:

1. **Top 10 Likely Questions** — what they'll probably ask, based on the role
   and what we know about the company. For each, suggest which anchor story
   to use.

2. **Your Top 5 Questions** — the highest-priority open questions Jeff should
   ask, given who he's meeting with.

3. **Key Talking Points** — 3-4 bullet points Jeff should weave into the
   conversation naturally.

4. **Watch For** — 2-3 signals to pay attention to during the conversation.

5. **Opening Hook** — a personalized 1-sentence opening that shows Jeff has
   done his research.

Format as clean, scannable text. No verbose paragraphs. This is a "glance
before you walk in" document.
"""
```

---

## 7. Data Flow Summary

```
LinkedIn/Careers Page
        │
        ▼
Google Sheets "To Evaluate" (Jeff drops URL)
        │
        ▼ (Watchdog or UI)
Scoring Engine (Deterministic → LLM)
        │
        ▼
Score written to Sheets + SQLite
        │
        ▼ (If score > 8.0)
Slack Notification
        │
        ▼ (Jeff promotes to pipeline)
Pipeline Stage: Researching
        │
        ▼
Research Agent runs → Strategy Brief created
Seed Questions generated → Question Bank populated
        │
        ▼
Pipeline Stage: Outreach → Recruiter → HM → Panel → Offer
        │
        │ (At each interview stage)
        ├── Cheat Sheet generated
        ├── Interview happens
        ├── Transcript pasted (optional)
        ├── Transcript analyzed
        ├── Questions updated (asked/answered/new)
        ├── Strategy Brief appended
        ├── Performance Scorecard appended
        └── Divergence checked (if applicable)
        │
        ▼
Terminal: Accepted / I Declined / They Declined
```
