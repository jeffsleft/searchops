# SearchOps: Reversing the Job Search

## What I was trying to solve

Job searching at scale is a numbers game played by someone else's rules. You submit an application, an ATS scores your resume against keywords, and somewhere in that black box, a hiring manager maybe sees your actual work history.

I was preparing for a CS Ops or GTM Ops leadership role and had a different problem: I didn't want to apply to 500 companies. I wanted to apply to maybe 30, high-quality fits where the company's stage, structure, and problems matched what I actually know how to solve. But I couldn't see those matches until I read the job description. And most job descriptions don't tell you what you need: the team size, the leverage points, whether the role is building systems or fixing a broken one.

I needed a reverse engine. Something that scored companies and roles against my specific criteria, career accomplishments, and what I'm looking for. That's SearchOps.

## Track A: What I built and where it broke

The first version was elegant in theory, useless in practice.

I built a scoring engine that took the JD text, pulled a generic 4-line candidate summary ("Finance Director with CS Ops background at GitLab"), ran it through Gemini with a bias prompt, and returned a score between 0 and 10. The LLM got about ±1.0 of adjustment authority. Everything else was deterministic: substring matches on tech stack, sector, remote work, company stage.

It was a spreadsheet with a chat window. I launched it, scored fifty roles, and realized the scores were garbage. An 8/10 could be a perfect match or a role that simply checked a few boxes. A 4/10 might be Director-level work I'd handled a dozen times, but in a sector I'd never worked.

The diagnostic was clear: the engine had no grounding in my actual accomplishments. It didn't know that I'd managed a $50M renewal cohort, or scaled CS operations from 2 to 15 people, or rebuilt finance workflow from zero at a nonprofit. So it couldn't connect that work to what the role actually needed. I could've tweaked the prompts. I spent two weeks chasing it. Instead, I scrapped it and rebuilt from the diagnosis.

## Track B: The rebuild

The second version centers on a corpus: my Accomplishments Inventory. A structured document listing every project, metric, and outcome from my career, not a resume but a reference table organized by function, stage, and outcome type.

Instead of a generic candidate summary, the scoring engine now indexes against this inventory. It pulls the JD, identifies requirements, maps them to specific accomplishments with evidence, and generates a 32-row evidence table: JD requirement → matched accomplishment → strength rating (weak/medium/strong). The LLM doesn't guess. It looks at proof.

Take a Director of Customer Success Operations role at a Healthcare SaaS company. Version 1 would've scored it on keyword overlap. Version 2 parsed the JD into 32 requirements and matched them to specific work from GitLab: scaling CS operations from 2→15 people, managing $72M→$550M ARR growth, building infrastructure for IPO readiness. Each requirement got a strength rating. That evidence table became the cover letter. The call came.

Layer 2 carries the highest weight in the architecture. It's grounded. The other layers are fast and brittle by design.

## The architecture: Four-layer scoring

**Layer 1: Auto-reject gate.** Some roles should score zero without touching the LLM. Sector blocklist (finance, recruiting, anything commoditized). Salary floor. Reporting line deal-breakers (reporting to a CFO when the role should have P&L autonomy). Fast. Cheap. Short-circuits to 0.0.

**Layer 2: Match to candidate.** One structured Gemini call. Feed the Accomplishments Inventory, the JD, and ask for a 32-row evidence table: each row maps a JD requirement to a specific accomplishment with a strength rating (weak/medium/strong). Also return tailored resume bullets and cover letter hooks. Structured output forces the model to account for every requirement, not just the easy ones. You see what doesn't match. This layer scores between –3.0 and +3.0 and carries the heaviest weight. The evidence table goes into the cover letter.

**Layer 3: LLM qualitative nudge.** IC versus leadership balance, strategy versus execution skew, reporting structure. Does the org shape make sense? This layer adds or subtracts up to ±1.0.

**Layer 4: Deterministic adjustments.** Substring matches on the JD, greenfield work, pricing model, and remote flexibility all feed in here, weighted up for greenfield. Brittle by design. These are adjustments, not the primary signal.

Final score clamped 0–10. Only 6 of 90 scored jobs cleared the 8+ bar.

## What's in the repo

The core is a Modal backend that runs the scoring pipeline. A SQLite database tracks jobs scraped from ATS feeds. A frontend built with HTMX and Jinja2 displays the results, the evidence tables, and the generated application materials.

What you fork is the scoring weights, the Accomplishments Inventory structure, the Hunt Targets list (the companies you actually want to work for), and the blocklists. The architecture is generic. The scoring outputs (evidence table, tailored resume bullets, cover letter hooks) are all queryable.

## Results

One hundred fifty-three jobs in the intake funnel. Ninety fully scored. Only 6 cleared the 8+ bar I set. Fifty-seven Tier A companies (Anthropic, Cohere, Weights & Biases, Retool, Temporal, Glean, Ramp, Vercel, Cloudflare, Notion, Zapier, Postman, Airtable, Pinecone, LangChain, Twilio, Okta) running daily ATS scans. Fifteen pipeline stages from Discovered through Offer/Decision.

Thirty companies in active conversation. Interview prep is automated: per-company question banks, red flags, anchor stories tied to role requirements. Application materials are generated per-company, grounded in evidence.

The search is narrow by design. The intake funnel is wide. The output is thirty companies worth pursuing. This is the method. Applied to CS Ops, it's renewal scoring, capacity planning, GTM measurement. Evidence over instinct. The framework doesn't change.

## The real product

I spent two weeks optimizing v1's score: tweaking prompts, adjusting weights, chasing the right number. The model ran fast, the scores looked plausible. But on real jobs? Useless.

An 8/10 could be a perfect match or keyword overlap. A 4/10 might be work I'd done a dozen times. The score had no grounding.

The diagnosis was clean: the score wasn't the product. The evidence table was. Not a number, but a 32-row breakdown of what matched and what didn't. That's what I needed to write a cover letter. That's what a recruiter would read.

When v2 shipped, the score became what it is: a sort key. The evidence table became everything. What gets sent to a recruiter, what shapes the cover letter, what makes the match real. The score ranks the jobs. The evidence table is why anyone reads the application.
