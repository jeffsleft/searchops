"""
All Gemini prompt templates. Import as named constants.
Prompts receive the candidate profile summary injected at call time.
"""

MATCH_PROMPT = """
You are scoring how well a SPECIFIC candidate matches a SPECIFIC job description, given
the candidate's full corpus of accomplishments, metrics, and stated differentiators.

This is the highest-weight signal in the scoring engine. Be honest. A score near 0
means "this candidate has not actually done what the role asks." A high positive
score requires explicit evidence in the corpus.

## Candidate Profile (preferences + identity)
---BEGIN UNTRUSTED INPUT---
{candidate_summary}
---END UNTRUSTED INPUT---

## Candidate Corpus (accomplishments, metrics, differentiators)
---BEGIN UNTRUSTED INPUT---
{corpus_text}
---END UNTRUSTED INPUT---

## Job Description
---BEGIN UNTRUSTED INPUT---
{jd_text}
---END UNTRUSTED INPUT---

## Your Task
Compare the JD's stated requirements and responsibilities against the corpus. For each
key JD requirement, find evidence (or its absence) in the corpus. Generic evidence
scores 0 or negative. Require specific JD requirement → named accomplishment + metric.
Then score.

Return a JSON object with these exact keys:

{{
  "match_score": <float between -3.0 and +3.0>,
  "match_summary": "<2-3 sentences: the overall fit story. Plain language.>",
  "evidence": [
    {{
      "jd_requirement": "<exact phrasing or close paraphrase from the JD>",
      "matched_accomplishment": "<the bullet/metric from the corpus that backs it>",
      "strength": "<Strong|Moderate|Weak>"
    }}
  ],
  "mismatches": [
    {{
      "jd_requirement": "<what the JD asks for>",
      "gap": "<what's missing or thin in the corpus>",
      "severity": "<High|Medium|Low>"
    }}
  ],
  "differentiator_themes": [
    "<theme from JD that resonates with one of Jeff's moats — e.g., 'AI-native ops', 'finance+ops bridge', 'consumption pricing fluency'>"
  ],
  "tailored_summary": "<2-3 sentence professional profile paragraph written specifically for this role and company. Written in first-person. Grounded in corpus evidence — reference specific accomplishments, metrics, or differentiators that match what this JD emphasizes most. This will replace the generic Profile section on the resume Jeff sends to this company.>",
  "tailored_bullets": [
    {{"company": "<company name from Jeff's experience exactly as it appears — e.g. GitLab, Mercy Ships, RightCapital>", "bullet": "<resume bullet in 'Lead label — specific detail with metrics' format. The lead label is 2-5 words (e.g. 'PROVE Health Engine', 'Renewal Operations', 'Digital CS'). Pulled or adapted from the corpus. Real metrics only — no hallucination.>"}}
  ],
  "cover_letter_hooks": [
    "<1 to 3 short sentences that could open or anchor a cover letter for this role, referencing specific corpus evidence>"
  ],
  "sections_to_drop": [
    "<exact section heading or company name to omit from the resume for this specific role. Only suggest entries that are clearly irrelevant — e.g. 'Ronald Blue' (audit background) for a modern GTM/CS Ops role, or 'RightCapital' if the role has no CS leadership angle. Never drop Profile, Core Competencies, GitLab, or Mercy Ships. Return an empty array if all sections are relevant.>"
  ]
}}

## Scoring Guidelines (match_score, -4.0 to +4.0)
+3.0 to +4.0  Corpus shows direct, recent, scaled evidence for the JD's core asks.
              Multiple Strong matches, no High-severity gaps. Requires specific named
              accomplishment with metric for each key requirement.
+1.5 to +2.9  Solid partial match. Key responsibilities have evidence, but at smaller
              scale, in adjacent domain, or with 1-2 medium gaps.
-1.0 to +1.4  Mixed. Some evidence, but meaningful gaps on what the JD emphasizes most.
-2.0 to -0.9  Mostly mismatch. The JD's core requirements lack corpus support, even if
              the candidate could "stretch" into the role.
-3.0 to -2.1  Fundamental mismatch — the role asks for things the candidate has never
              done, at any scale, in any related domain.
-4.0 to -3.1  Anti-match: role directly conflicts with candidate's stated preferences
              or requires skills explicitly excluded from the profile.

If the corpus is empty or unavailable, return match_score = 0.0, evidence = [],
mismatches = [{{"jd_requirement": "(corpus not available)", "gap": "Layer 2 cannot run without the Accomplishments Inventory.", "severity": "High"}}], and empty arrays elsewhere.

Cite the corpus literally. Do not invent metrics. Use "(none found in corpus)" if there
is no evidence for something the JD asks for.

Return ONLY valid JSON. No markdown fences.
"""


SCORING_PROMPT = """
You are doing a qualitative role-shape assessment for a candidate. The corpus-based
"match" check has already run — your job is the *role-shape vibe*: does this role have
the right structure regardless of whether the candidate has the right experience?

## Candidate Profile
---BEGIN UNTRUSTED INPUT---
{candidate_summary}
---END UNTRUSTED INPUT---

## Job Description
---BEGIN UNTRUSTED INPUT---
{jd_text}
---END UNTRUSTED INPUT---

## Adjustment Signals From Earlier Layers
Adjustment weights score: {deterministic_score}/10
Flags detected: {flags}

## Your Task

FIRST: Check whether the Job Description section above contains an actual job posting
with role-specific content (responsibilities, requirements, company context). If the
text is a login page, a "JavaScript required" stub, a generic careers page, a 404/error
page, or otherwise lacks enough content to evaluate the role, set "jd_insufficient"
to true and return the minimal JSON below — do NOT fabricate a score.

If the JD is sufficient, read it for role *shape* — authority, scope, IC vs leadership
balance, reporting line, and any culture/business-model red flags that the keyword layers
cannot catch. Return a JSON object with these exact keys:

{{
  "jd_insufficient": false,
  "llm_adjustment": <float between -1.5 and +1.5>,
  "company": "<company name extracted from JD>",
  "job_title": "<job title extracted from JD>",
  "pros": "<4-5 lines: what about the role's *shape* fits the candidate>",
  "cons": "<4-5 lines: shape-level concerns or risks>",
  "greenfield": "<Yes|No|Partial>",
  "greenfield_rationale": "<1 sentence explaining the greenfield assessment>",
  "pricing_model": "<Seat|Consumption|Outcome|Hybrid|Unknown>",
  "sector": "<primary sector classification>",
  "posting_date_raw": "<date string extracted from JD, e.g. 'March 15, 2026', or null>",
  "posting_age_days": <integer days since posting, or null if undetectable>,
  "role_archetype": "<GTM Ops|CS Ops|RevOps|Finance Ops|Strategy|IC-Heavy|Other>",
  "role_shape": {{
    "ic_vs_leadership": "<Heavy IC|Balanced|Heavy Leadership|Unknown>",
    "team_size_to_lead": "<number, range, or Unknown>",
    "reporting_line": "<CEO|CRO|CFO|CCO|COO|VP Eng|Unknown>",
    "strategic_vs_execution": "<Strategy-Heavy|Balanced|Execution-Heavy|Unknown>"
  }},
  "recommended_angle": "<1 sentence: how should the candidate position themselves?>",
  "tech_stack_detected": {{
    "crm": "<Salesforce|HubSpot|Other|Unknown>",
    "cs_tool": "<Gainsight|Vitally|Totango|Other|Unknown>",
    "comms": "<Slack|Teams|Other|Unknown>",
    "os": "<Mac|Windows|Unknown>",
    "cloud": "<AWS|GCP|Azure|Unknown>"
  }},
  "salary_range_detected": "<string or null>",
  "has_fde_model": "<Yes|No|Unknown>",
  "interview_probability": "<Low|Medium|High — probability a cold application gets a screening call>",
  "interview_probability_rationale": "<1 sentence: key factor driving this estimate>"
}}

If the JD is insufficient, return ONLY this:
{{
  "jd_insufficient": true
}}

## Adjustment Guidelines (llm_adjustment, -1.5 to +1.5)
Adjust UP (+1.0 to +1.5) if:
- Role has real authority — P&L, headcount, strategy ownership
- High-growth company with modern GTM thinking
- AI/automation is central to the role, not an afterthought
- Reporting line and team size suggest a true leadership seat

Adjust DOWN (-1.0 to -1.5) if:
- JD reads as a "firefighter" role — all tactical, no strategic
- Heavily IC despite the leadership title
- Company seems to be in decline or pivoting away from CS
- Red flags in culture, leadership, or business model

Keep at 0 if signals balance out or there is insufficient information.

## Interview Probability Assessment
Estimate the probability that a cold application to this role results in a screening call.
- High: role is clearly hiring, company is growing, JD is specific and active
- Medium: role may be active but signals are mixed (older posting, vague requirements, etc.)
- Low: role looks like a ghost posting, company has red flags, or JD is very generic
Base this ONLY on what's in the JD — do not factor in the candidate's fit.

## Posting Date Extraction
Scan the JD for any posting date, "posted X days ago", or "updated" timestamp. Extract the raw string and estimate days since posting. Set null if no date signal found.

## Role Archetype Classification
Classify the role into exactly one archetype (GTM Ops, CS Ops, RevOps, Finance Ops, Strategy, IC-Heavy, or Other) based on its primary function and scope.

Return ONLY valid JSON. No markdown fences.
"""


RESEARCH_PROMPT = """
You are performing targeted due diligence on a company for Jeff Beaumont — a
GTM Operations & Strategy Leader (VP/Director level) evaluating proactive outreach
opportunities.

## Jeff's Identity & Values
- ROLE: GTM Operations & Strategy (Not a Systems Architect/SFDC Admin).
- VALUES: Greenfield builds, "Buy AND Build" AI philosophy, and strategic influence.
- DISLIKES: Maintenance-only roles, IC-heavy presentation work (Veradata/IC-slideware).

## Company to Research
{company_name}

Use web search and LinkedIn to find current information. If you cannot find reliable
data for a field, use "Unknown" rather than guessing.

## Step 1 — Current Ops Leader
Search LinkedIn and the web to find whether this company currently has someone in a
GTM Ops, RevOps, or CS Ops leadership role. Valid titles include (but are not limited
to): Director of CS Operations, Sr. Director GTM Ops, VP RevOps, Head of Revenue
Operations, VP Operations, Senior Director Revenue Strategy and Operations, Senior
Director Strategy and Operations, Head of Global Customer Success Operations, Vice
President of GTM Operations, SVP Revenue Operations, Director of Business Operations.
Titles using "Vice President" in full (not VP) and "Senior Vice President" also count.

- If you find a current person in such a role: set ops_leader_name to their full name,
  ops_leader_linkedin to their LinkedIn URL in the format linkedin.com/in/[username]
  (leave blank if you can only find a search results page, not a direct profile URL).
- If you cannot confidently identify a current ops leader: set ops_leader_name to "N",
  ops_leader_linkedin to "".

## Step 2 — Open CS-Related Roles
Search the company's website (especially its careers or jobs page) for open roles in
any of these categories: Customer Success Manager, Technical Account Manager, Forward
Deployed Engineer, Professional Services (consultant, engineer, implementation, etc.),
Deployment Engineer, Implementation Engineer, Renewals Manager, Customer Success
Engineer. Note: Account Manager is NOT a CS role.

- If at least one such role is open: cs_roles_open = "Y", cs_roles_list = comma-separated
  list of the role titles found.
- If none found: cs_roles_open = "N", cs_roles_list = "".

## Step 3 — LinkedIn Headcount
Find the company's LinkedIn page and navigate to the About tab. It shows two numbers:
a specific member count and a range (e.g. "483 associated members on LinkedIn · 51-200
employees"). Use ONLY the specific number (e.g. "483"), not the range.

## Step 4 — Work Arrangement
Review job descriptions to determine if the company is remote-first, hybrid, or
in-office. For in-office, include known office locations.

## Step 5 — Standard Research Fields
Also gather: funding stage, total raised, headcount trend, revenue model, pricing model,
HQ location, customer segments, CEO/founder type, exec team summary, tech stack,
top competitors, competitive position, Glassdoor sentiment, red flags, estimated runway,
whether they have an FDE model, and whether CS team is shrinking while sales is growing.

## Step 6 — Outreach Timing Signal
Search for evidence of recent events that often trigger GTM Ops / CS Ops hiring:
- A key exec (CEO, CRO, CCO, COO, VP CS, VP Sales) was promoted in the last 6 months
- A new leader joined (new hire in any of those roles)
- A new funding round closed in the last 6 months
If any of these signals are present, set timing_signal = "Yes" with a specific rationale naming the event and when it happened.
If none found: timing_signal = "No". If data is insufficient: "Unknown".

Return a JSON object with these exact keys:

{{
  "ops_leader_name": "<full name or N>",
  "ops_leader_linkedin": "<linkedin.com/in/username or empty string>",
  "cs_roles_open": "<Y|N>",
  "cs_roles_list": "<comma-separated role titles or empty string>",
  "linkedin_headcount": "<specific number as string, e.g. 483, or Unknown>",
  "work_arrangement": "<remote|hybrid|in-office (locations)|Unknown>",
  "funding_stage": "<Seed|Series A|B|C|D|Late Stage|Public|Bootstrapped|Unknown>",
  "total_raised": "<dollar amount or Unknown>",
  "headcount": "<number or range or Unknown>",
  "headcount_trend": "<Growing|Flat|Shrinking|Unknown>",
  "cs_team_trend": "<Growing|Flat|Shrinking|Unknown - relative to Sales team>",
  "revenue_model": "<PLG|SLG|Hybrid|Unknown>",
  "pricing_model": "<Seat|Consumption|Outcome|Hybrid|Unknown>",
  "hq_location": "<city, state/country or Unknown>",
  "customer_segments": "<Enterprise|Mid-Market|SMB|Mixed|Unknown>",
  "ceo_founder_type": "<1st-time Founder|2nd-time Founder|Professional CEO|Unknown>",
  "exec_team": "<2-3 sentence summary of CEO, CRO, COO, CFO — names, backgrounds, notable experience>",
  "tech_stack": "<brief summary of known tech stack or Unknown>",
  "competitors": "<top 3 competitors comma-separated or Unknown>",
  "competitive_position": "<1-2 sentences: how they rank vs competitors, key differentiators>",
  "glassdoor_sentiment": "<Positive|Mixed|Negative|Unknown — 1 sentence summary of themes>",
  "red_flags": "<layoffs, bad Glassdoor, pivot, leadership churn, etc. or None detected>",
  "estimated_runway": "<months or Unknown>",
  "has_fde_model": "<Yes|No|Unknown>",
  "cs_shrinking_sales_growing": <true|false>,
  "outreach_hook": "<1 sentence: personalized hook referencing something specific and current>",
  "signals": [
    {{"severity": "warn|info|good", "text": "<one concrete, time-stamped or sourced observation — a fact, not an opinion>"}}
  ],
  "strategy_vs_ic": "<Strategy-Heavy|Balanced|IC-Heavy - assess based on JD/role expectations>",
  "is_greenfield": <true|false - is this building something new or maintaining an existing machine?>,
  "timing_signal": "<Yes|No|Unknown — is now a good time to reach out based on recent events?>",
  "timing_signal_rationale": "<1 sentence: what specific event (promo, new hire, new round) drives this signal, or why Unknown>",
  "industry": "<short industry label, e.g. DevTools, AI Infrastructure, FinTech, PropTech, MarTech, RevOps/CS Tooling, Data & Analytics, Cybersecurity, HR Tech, EdTech, HealthTech SaaS, or best-fit label>"
}}

Return ONLY valid JSON. No markdown fences.
"""


COMPANY_FIT_PROMPT = """
You are assessing whether a company is likely to need someone with this candidate's
profile, even if no specific role is currently posted.

## Candidate Profile
---BEGIN UNTRUSTED INPUT---
{candidate_summary}
---END UNTRUSTED INPUT---

## Company Research
---BEGIN UNTRUSTED INPUT---
{research_json}
---END UNTRUSTED INPUT---

## Your Task
Assess two things:

1. FIT: How well does this company match the candidate's preferences?
   Consider: sector, stage, tech stack, pricing model, greenfield opportunity.

2. NEED: Is this company likely to need a VP/Director-level GTM/CS Ops leader?
   Consider: headcount (200-500 is prime), growth trajectory, operational maturity,
   whether they'd benefit from the candidate's specific background.

Return a JSON object:

{{
  "fit_score": <float 0-10>,
  "fit_rationale": "<1-2 sentence high-level summary of fit>",
  "fit_justification": [
    "<3-5 specific bullet points justifying the fit score based on Jeff's criteria>"
  ],
  "need_assessment": "<High|Medium|Low|Unknown>",
  "need_rationale": "<1-2 sentence summary of why they likely need (or don't need) a senior operator>",
  "need_justification": [
    "<2-3 bullet points justifying the need assessment (e.g., headcount growth, lack of current leader, etc.)>"
  ],
  "outreach_recommended": <true|false>,
  "outreach_angle": "<1 sentence: the hook for a cold outreach if recommended>"
}}

Return ONLY valid JSON. No markdown fences.
"""


TRANSCRIPT_ANALYSIS_PROMPT = """
Analyze this interview transcript. Assess it from the candidate's perspective — what
was learned about the company, what signals emerged, and how did the candidate perform.

## Candidate: {candidate_name}
## Company: {company}
## Role: {job_title}
## Interviewer(s): {interviewers}

## Candidate Profile (Summary)
---BEGIN UNTRUSTED INPUT---
{candidate_summary}
---END UNTRUSTED INPUT---

## Transcript
---BEGIN UNTRUSTED INPUT---
{transcript_text}
---END UNTRUSTED INPUT---

## Existing Questions for This Company
---BEGIN UNTRUSTED INPUT---
{existing_questions_json}
---END UNTRUSTED INPUT---

Return a JSON object:

{{
  "unanswered_questions": [
    {{
      "question": "<question raised but not answered>",
      "category": "<Financial|Strategic|Technical|Cultural|Pricing|Operational>",
      "priority": "<High|Medium|Low>",
      "context": "<why this matters based on the transcript>"
    }}
  ],
  "new_questions_to_ask": [
    {{
      "question": "<new question surfaced from this conversation>",
      "category": "<category>",
      "priority": "<priority>",
      "persona_target": "<CFO|CRO|COO|CCO|VP Eng|Founder|Recruiter|Any>",
      "reason": "<why this question matters>"
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
    "persona_type": "<The Technical Skeptic|The Visionary Founder|The Financial Operator|The Growth Evangelist|The Process Fixer|The Culture Guardian>",
    "evidence": "<1-2 sentences explaining the classification>"
  }},
  "jeff_performance": {{
    "strong_moments": ["<specific things the candidate said well>"],
    "weak_moments": ["<where the candidate was vague or missed an opportunity>"],
    "anchor_stories_used": ["<which anchor stories were told>"]
  }},
  "key_signals": ["<important information learned about the company>"]
}}

Return ONLY valid JSON. No markdown fences.
"""


TRANSCRIPT_COMPARISON_PROMPT = """
You have just analyzed an interview transcript independently. Now compare your analysis
to the Granola AI summary provided below.

## Your Independent Analysis
---BEGIN UNTRUSTED INPUT---
{gemini_analysis}
---END UNTRUSTED INPUT---

## Granola's Analysis
---BEGIN UNTRUSTED INPUT---
{granola_analysis}
---END UNTRUSTED INPUT---

Identify:
- Where you agree (same signals, same observations)
- What you caught that Granola missed (your analysis only)
- What Granola caught that you missed (Granola only)
- Where you actively diverge (different interpretations of the same event)

Return a JSON object:

{{
  "agreements": ["<things both analyses agree on>"],
  "gemini_only": ["<signals/observations in your analysis not in Granola>"],
  "granola_only": ["<signals/observations in Granola not in your analysis>"],
  "divergences": [
    {{
      "topic": "<what you diverge on>",
      "gemini_view": "<your interpretation>",
      "granola_view": "<Granola's interpretation>",
      "recommendation": "<trust_gemini|trust_granola|investigate>"
    }}
  ],
  "overall_recommendation": "<trust_gemini|trust_granola|investigate|both_useful>",
  "summary": "<2-3 sentences: net takeaway from the comparison>"
}}

Return ONLY valid JSON. No markdown fences.
"""


DIVERGENCE_PROMPT = """
The same question was asked to two different people at {company}.

Question: {question}

{person_a_title} ({person_a_name}):
---BEGIN UNTRUSTED INPUT---
{answer_a}
---END UNTRUSTED INPUT---

{person_b_title} ({person_b_name}):
---BEGIN UNTRUSTED INPUT---
{answer_b}
---END UNTRUSTED INPUT---

Assess:
1. Do they fundamentally agree or diverge?
2. If divergent, what does this suggest about the organization?
3. Is this a red flag (antagonistic relationship) or healthy tension?

Return a JSON object:

{{
  "aligned": <true|false>,
  "summary": "<2-3 sentences explaining the alignment or divergence>",
  "red_flag": <true|false>,
  "red_flag_reason": "<1 sentence if red_flag is true, else null>"
}}

Return ONLY valid JSON. No markdown fences.
"""


CHEATSHEET_PROMPT = """
Generate an interview preparation cheat sheet for {candidate_name} interviewing
at {company} for {job_title}.

## Company Research
---BEGIN UNTRUSTED INPUT---
{research_summary}
---END UNTRUSTED INPUT---

## Strategy Brief (Current State)
---BEGIN UNTRUSTED INPUT---
{strategy_brief}
---END UNTRUSTED INPUT---

## Upcoming Interview
Round: {round}
Interviewer(s): {interviewers}

## Candidate's Anchor Stories
---BEGIN UNTRUSTED INPUT---
{anchor_stories}
---END UNTRUSTED INPUT---

## Open Questions (High Priority)
---BEGIN UNTRUSTED INPUT---
{high_priority_questions}
---END UNTRUSTED INPUT---

Generate a concise, scannable cheat sheet:

1. **Top 10 Likely Questions** — what they'll probably ask. For each, suggest which
   anchor story to use.

2. **Your Top 5 Questions** — highest-priority open questions for this round, given
   who the candidate is meeting with.

3. **Key Talking Points** — 3-4 bullets to weave in naturally.

4. **Watch For** — 2-3 signals to pay attention to during this conversation.

5. **Opening Hook** — 1 personalized sentence showing the candidate has done their
   homework on this company.

Format as clean scannable text. No verbose paragraphs. This is a glance-before-you-walk-in document.
"""


STRATEGY_BRIEF_UPDATE_PROMPT = """
You are updating a Strategy Brief for {company} after a new interview.

## Existing Brief
---BEGIN UNTRUSTED INPUT---
{current_brief}
---END UNTRUSTED INPUT---

## New Interview Data
Date: {date}
Contact: {contact_name} ({contact_title})
Round: {round}
Candidate Notes: 
---BEGIN UNTRUSTED INPUT---
{jeff_notes}
---END UNTRUSTED INPUT---

Transcript Analysis: 
---BEGIN UNTRUSTED INPUT---
{transcript_analysis}
---END UNTRUSTED INPUT---

## Task
1. Add a new "Interview Intelligence" section entry for this conversation.
2. Update "Operational Debt Assessment" if new signals were detected.
3. Update "Metrics & Pricing Model" if new information surfaced.
4. If this is the 2nd or later interview, generate or update the "Recommendation" section.

Return ONLY the NEW SECTIONS as markdown. Do not repeat unchanged sections.
Each section should use the appropriate heading level (##, ###).
"""


MOCK_QUESTIONS_PROMPT = """
Generate likely interview questions for Jeff Beaumont interviewing for {job_title}
at {company}. Jeff is a Senior Director/VP-level GTM Operations, Revenue Operations,
and CS Operations leader.

## Company Research
---BEGIN UNTRUSTED INPUT---
{research_summary}
---END UNTRUSTED INPUT---

## Role Analysis
---BEGIN UNTRUSTED INPUT---
{role_analysis}
---END UNTRUSTED INPUT---

## Jeff's Anchor Stories
---BEGIN UNTRUSTED INPUT---
{anchor_stories}
---END UNTRUSTED INPUT---

For each of the top 10 likely questions:
- State the question
- Identify which anchor story best answers it
- Note the key point to make (1 line)

Focus on questions that probe: operational scaling, AI-native systems, GTM
infrastructure, team building, metrics fluency (NRR, GRR, consumption models),
and greenfield build experience.

Format as a clean numbered list. Keep each entry to 3 lines max.
"""


INTERVIEWER_RESEARCH_PROMPT = """
You are preparing Jeff Beaumont for a senior leadership interview. Research the
interviewer and provide a practical, executive-level brief.

## Interviewer
Name: ---BEGIN UNTRUSTED INPUT---
{interviewer_name}
---END UNTRUSTED INPUT---

Title: ---BEGIN UNTRUSTED INPUT---
{interviewer_title}
---END UNTRUSTED INPUT---

Company: ---BEGIN UNTRUSTED INPUT---
{company}
---END UNTRUSTED INPUT---

Use web search to find current and accurate information. If you cannot find reliable
data for a section, say so — do not fabricate quotes or details.

Return a JSON object with these exact keys:

{{
  "background": {{
    "summary": "<2-3 sentence career trajectory and key roles>",
    "notable_companies": "<companies/roles most relevant to this conversation>",
    "conversation_approach": "<1-2 sentences: what their background suggests about how they'll engage>"
  }},
  "leadership_philosophy": {{
    "summary": "<what they appear to value most in leaders and teams>",
    "public_quotes": ["<direct quote or paraphrase with source>"],
    "focus_areas": ["<CS leadership>", "<GTM/RevOps/Ops leadership>", "<AI>"]
  }},
  "strategy_perspective": {{
    "summary": "<their stated views on company-building, scaling, or market positioning>",
    "quotes": ["<specific quote or paraphrase with source>"]
  }},
  "hiring_signals": {{
    "what_they_emphasize": "<execution, talent density, customer obsession, technical depth, etc.>",
    "stated_hiring_views": "<any public statements on hiring or leadership expectations>"
  }},
  "likely_interview_focus": {{
    "probable_topics": ["<topics they are likely to probe given their background and the role>"],
    "likely_questions": ["<3-5 specific questions they may ask>"],
    "what_success_looks_like": "<1-2 sentences on what they probably want from this hire>"
  }},
  "engagement_strategy": {{
    "how_to_resonate": "<data-driven|vision-oriented|customer-first|execution-focused — explain>",
    "rapport_topics": ["<topics or angles likely to build connection>"],
    "questions_to_ask_them": [
      "<question 1 that aligns with their interests>",
      "<question 2>",
      "<question 3>",
      "<question 4>",
      "<question 5>",
      "<question 6>",
      "<question 7>"
    ]
  }}
}}

Return ONLY valid JSON. No markdown fences.
"""


INTERVIEW_COACHING_PROMPT = """
You are an executive interview coach reviewing Jeff Beaumont's interview performance.
Jeff is interviewing for Senior Director/VP-level GTM Operations, Revenue Operations,
and CS Operations roles.

## Interview Context
Company: {company}
Role: {job_title}
Interviewer: {interviewer}
Date: {date}

## Transcript
---BEGIN UNTRUSTED INPUT---
{transcript_text}
---END UNTRUSTED INPUT---

## Instructions
Evaluate ONLY Jeff's responses — not the interviewer's questions or comments.
For each substantive response Jeff gives, score it and provide coaching.

Then provide aggregate stats and an overall assessment.

Return a JSON object:

{{
  "per_response": [
    {{
      "response_number": <int>,
      "question_asked": "<brief summary of what was asked>",
      "jeff_response_summary": "<1-2 sentence summary of what Jeff said>",
      "scores": {{
        "overall": <1-10>,
        "content": <1-10>,
        "structure": <1-10>,
        "delivery": <1-10>
      }},
      "did_well": ["<specific strength 1>", "<specific strength 2>"],
      "improve": ["<specific improvement 1>", "<specific improvement 2>"],
      "anchor_story_used": "<story id or null>",
      "anchor_story_recommended": "<better story to use, or null>"
    }}
  ],
  "filler_words": {{
    "total_words_spoken": <int or null if not countable>,
    "total_fillers": <int>,
    "filler_rate_pct": <float>,
    "breakdown": {{
      "um": <int>,
      "uh": <int>,
      "like": <int>,
      "you_know": <int>,
      "so": <int>,
      "i_mean": <int>,
      "right": <int>
    }},
    "top_habits": ["<filler word> — <one-line suggestion to reduce it>"]
  }},
  "overall": {{
    "scores": {{
      "overall": <1-10>,
      "content": <1-10>,
      "structure": <1-10>,
      "delivery": <1-10>
    }},
    "top_strengths": ["<strength with specific example from transcript>"],
    "top_improvements": ["<improvement with concrete suggestion>"],
    "interviewer_sentiment": "<Positive|Neutral|Negative|Unknown>",
    "sentiment_evidence": "<what in the transcript supports this assessment>",
    "anchor_stories_that_landed": ["<story ids>"],
    "missed_opportunities": ["<moments where a stronger answer was available>"]
  }}
}}

Scoring rubric:
- Content (1-10): clarity, relevance, depth, specificity, use of data/metrics
- Structure (1-10): logical flow, clear beginning/middle/end, SAR or PPP framework
- Delivery (1-10): pacing, confidence, conciseness, filler word frequency
- Overall: weighted average, with content weighted most heavily

Be specific. Reference exact things Jeff said. Avoid vague praise.
Return ONLY valid JSON. No markdown fences.
"""


COVER_LETTER_PROMPT = """
You are drafting a full cover letter for a SPECIFIC candidate applying to a SPECIFIC
role. Write the actual letter — not hooks, not bullet points, not a template. The
output is a finished letter the candidate could send after a light edit.

## Candidate Profile (preferences + identity)
---BEGIN UNTRUSTED INPUT---
{candidate_summary}
---END UNTRUSTED INPUT---

## Candidate Corpus (accomplishments, metrics, differentiators — cite literally)
---BEGIN UNTRUSTED INPUT---
{corpus_text}
---END UNTRUSTED INPUT---

## Job Description
---BEGIN UNTRUSTED INPUT---
{jd_text}
---END UNTRUSTED INPUT---

## Company
{company}

## Company Research (may be empty)
---BEGIN UNTRUSTED INPUT---
{research_summary}
---END UNTRUSTED INPUT---

## Prior Match Signal (the strongest JD→evidence connections already found — reuse these)
---BEGIN UNTRUSTED INPUT---
{match_signal}
---END UNTRUSTED INPUT---

## Voice — write the way this candidate actually writes, not the way an AI writes
- Open with a punchy, specific hook. Name something real and current about this company
  (from the research or JD). No "I am writing to express my interest in..."
- Full sentences with connected clauses. Do NOT stack staccato fragments. This is a
  letter, not a deck.
- Honest framing. Humble verbs ("helped," "led," "built") over inflated ones
  ("spearheaded," "transformed," "revolutionized").
- Concrete stakes over adjectives. Show the work; don't editorialize about it.
- Say "my team," not "the team," when describing people the candidate led.
- Cut connective tissue that only an AI writes: "I am confident that," "I believe my
  experience makes me," "This is why I am excited about the opportunity to."
- Do NOT borrow the company's own marketing language back at them. Do NOT pile up
  resume metrics — the resume already carries those; the letter carries judgment and fit.
- A dry aside or a flash of humor is welcome if it lands. Forced enthusiasm is not.
- 3 to 4 paragraphs. Tight. A hiring manager reads the first two lines and the last one.

## Your Task
Write the letter grounded in real corpus evidence for THIS role. Reference specific
accomplishments only where they answer something the JD actually asks for. If the
research is empty, lean on the JD for the company-specific hook. Never invent metrics,
titles, or facts not present in the corpus or research.

Return a JSON object with these exact keys:

{{
  "recipient": "<addressee block, e.g. 'Hiring Team, {company}' — one line, or a named person if the research surfaces the hiring manager>",
  "salutation": "<e.g. 'Dear Hiring Team,' or 'Dear [Name],' — match the recipient>",
  "body": [
    "<paragraph 1 — the hook + why this company, specifically>",
    "<paragraph 2 — the strongest fit: JD requirement met with real evidence, in prose>",
    "<paragraph 3 — a second angle (judgment, build experience, or a differentiator)>",
    "<paragraph 4 (optional) — the close: a clear, low-pressure ask>"
  ],
  "closing": "Sincerely,"
}}

Return ONLY valid JSON. No markdown fences.
"""
