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

Instead of a generic candidate summary, the scoring engine now indexes against this inventory. It pulls the JD, identifies requirements, maps them to specific accomplishments with evidence, and generates an evidence table — one row per JD requirement (they run anywhere from 6 to 32 rows depending on the JD): JD requirement → matched accomplishment → strength rating (Strong/Moderate/Weak). The LLM doesn't guess. It looks at proof.

A real one: a Senior Director, Strategy and Operations role at Decagon, an AI customer-experience company. Version 1 would've scored it on keyword overlap. Version 2 parsed the JD into 14 requirements and matched them row by row to specific work — "develop repeatable processes that improve deployment consistency" mapped to architecting the CS Ops infrastructure behind a 68% YoY revenue surge at GitLab; "operating rhythms and reporting that create visibility into delivery health" mapped to deploying Gainsight across a 250-person CS org and building the company's churn analysis by industry, region, segment, and cohort. Thirteen matches, one honest mismatch (location preference). Match score: +3.7 of a possible +4.0. Final: 9.7 — one of the very few roles to clear the top-band gate.

Layer 2 carries the highest weight in the architecture. It's grounded. The other layers are fast and brittle by design.

## The architecture: Four-layer scoring

**Layer 1: Auto-reject gate.** Some roles should score zero without touching the LLM. Sector blocklist (in my config: government, healthcare, adult content, gambling — yours would differ). Salary floor ($175k base in my config; roles with no salary listed get the benefit of the doubt). Fast. Cheap. Short-circuits to 0.0.

**Layer 2: Match to candidate.** One structured Gemini call. Feed the Accomplishments Inventory, the JD, and ask for an evidence table: each row maps a JD requirement to a specific accomplishment with a strength rating (Strong/Moderate/Weak). Also return tailored resume bullets and cover letter hooks. Structured output forces the model to account for every requirement, not just the easy ones. You see what doesn't match. This layer scores between –4.0 and +4.0 and carries the heaviest weight. The evidence table goes into the cover letter.

**Layer 3: LLM qualitative nudge.** IC versus leadership balance, strategy versus execution skew, reporting structure. Does the org shape make sense? This layer adds or subtracts up to ±1.5.

**Layer 4: Deterministic adjustments.** Substring matches on the JD, greenfield work, pricing model, and remote flexibility all feed in here, weighted up for greenfield. Brittle by design. These are adjustments, not the primary signal — the positive total is capped at +2.5, so surface signals alone can't carry a role past 6.0.

Final = base 3.5 + Layer 4 + Layer 2 + Layer 3, clamped 0–10 once at the end. One more brake: any score above 7.0 has to pass five must-haves (comp on target, a real leadership seat, an acceptable sector, a build mandate, strong Layer 2 evidence) or it's clamped back to 7.0. An 8+ is rare and earned by design.

## What's in the repo

The core is a Modal backend that runs the scoring pipeline. A SQLite database tracks jobs scraped from ATS feeds. A frontend built with HTMX and Jinja2 displays the results, the evidence tables, and the generated application materials.

What you fork is the scoring weights, the Accomplishments Inventory structure, the Hunt Targets list (the companies you actually want to work for), and the blocklists. The architecture is generic. The scoring outputs (evidence table, tailored resume bullets, cover letter hooks) are all queryable.

## Results

One hundred sixty-eight jobs in the intake funnel. Ninety-one fully scored. Fifty-eight Tier A companies (Anthropic, Cohere, Weights & Biases, Retool, Temporal, Glean, Ramp, Vercel, Cloudflare, Notion, Zapier, Postman, Airtable, Pinecone, LangChain, Twilio, Okta among them) running daily ATS scans. Fifteen pipeline stages from Discovered through Offer/Decision.

The June scoring overhaul reset the bar — base dropped to 3.5, Layer 4 capped, top-band gate added — so a score above 7 now means something. Scores from the earlier engine stay on the old scale until they're re-run; the engine keeps per-job score history so the two eras don't get conflated. Under the current engine, clearing 8+ looks like the Decagon example above: strong evidence on nearly every requirement, plus all five gate must-haves.

Applications go out one at a time, each grounded in its evidence table. Interview prep is automated: per-company question banks, red flags, anchor stories tied to role requirements. Application materials are generated per-company, grounded in evidence.

The search is narrow by design. The intake funnel is wide. The output is a short list worth pursuing. This is the method. Applied to CS Ops, it's renewal scoring, capacity planning, GTM measurement. Evidence over instinct. The framework doesn't change.

## The real product

I spent two weeks optimizing v1's score: tweaking prompts, adjusting weights, chasing the right number. The model ran fast, the scores looked plausible. But on real jobs? Useless.

An 8/10 could be a perfect match or keyword overlap. A 4/10 might be work I'd done a dozen times. The score had no grounding.

The diagnosis was clean: the score wasn't the product. The evidence table was. Not a number, but a row-by-row breakdown of what matched and what didn't. That's what I needed to write a cover letter. That's what a recruiter would read.

When v2 shipped, the score became what it is: a sort key. The evidence table became everything. What gets sent to a recruiter, what shapes the cover letter, what makes the match real. The score ranks the jobs. The evidence table is why anyone reads the application.

## How this was built

I'm not a developer. I direct AI agents that write every line, and the interesting part isn't that — it's the control system around them, because an unsupervised agent will happily tell you it's done when it isn't.

The rebuild ran as 14 self-contained work packages ([the actual build spec is in the repo](build/searchops-rebuild.md)) — each one a single session with its own acceptance criteria, verification steps, and scope discipline ("execute ONLY the named WP"). Architecture calls that outlived a session went into [ADRs](adr/decisions.md). Every session ended by updating a handoff doc and a status tracker so the next session — often a different model — could pick up cold.

Two rules did most of the work:

**Verify before you change.** Agents quote code from memory and search excerpts, and they're confidently wrong just often enough to be dangerous. Every work package starts with "read the actual file first." The same rule applies to documentation: every number in this case study was checked against the production database before it was written, and when the data said one of my own claims was stale, the claim changed, not the data.

**An agent's "done" is a claim, not a fact.** The sharpest example: an agent building the progress-instrumentation feature reported "ready for review" with passing tests. Line-by-line review found a nested database-connection deadlock that would have silently broken outcome recording on every terminal pipeline transition in production — confirmed empirically, the error swallowed by a bare `except` — plus four of six required tests stubbed out with skips. The fix took one more commit. Catching it took reading the diff instead of the summary.

That's the actual method on display in this repo: define done in measurable terms, delegate the typing, verify the claims, keep the receipts. It's the same discipline as running a revenue operation — the agents are just faster to hire.
