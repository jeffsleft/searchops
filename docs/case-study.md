# SearchOps: The Job Search, Reversed

**Companies score candidates. I built a system that scores the companies.**

| | |
|---|---|
| **What** | An applicant tracking system, run in reverse. |
| **How** | Each role is scored against proof from my career, not keywords. |
| **Result** | 370 roles found. 47 scored 8 or higher. 19 applications sent. |

## Why applying everywhere doesn't work

Hiring software screens people on keywords. I wanted the opposite: a short list of roles where my record fits the work. Thirty strong applications beat 500 blind ones.

Job posts skip what matters, like team size, company stage, and whether the job is to build or to repair. I needed to read hundreds of posts and find the few worth a letter.

## Why version one failed

Version one gave an AI model a four-line summary of me and asked for a score. It scored fifty roles in an afternoon. The scores were noise. An 8 could be a real fit or a lucky keyword match. A 4 could be work I had done a dozen times.

I spent two weeks tuning prompts and weights before I admitted the problem. Oops. The model had never seen my work, so it had nothing to reason from.

## Version two: an evidence table

Version two starts from an inventory of my career. Every project, metric and result, organized for lookup.

For each role, the system breaks the job post into requirements. It matches each one to a specific accomplishment and rates the match strong, moderate or weak. Gaps show in plain sight.

A real example: Senior Director of Strategy and Operations at Decagon. The system found 14 requirements. Thirteen matched real work, such as the operations behind 68% year-over-year revenue growth at GitLab. One did not: location. The role scored 9.7.

The score sorts the list. The evidence writes the application.

## How a role gets scored

1. **Rules first.** Wrong sector or pay below my floor: zero. No AI needed.
2. **Evidence.** The career match above. It carries the most weight.
3. **Judgment.** The AI weighs the shape of the role: leadership seat, reporting line, build or maintain.
4. **Signals.** Quick checks for remote work, pricing model and greenfield builds. Capped, so they cannot carry a weak role.

One last gate. No role scores above 7 unless it meets five must-haves, from pay to a real mandate to build.

## Results so far

As of September 24, 2026, from the production database:

- 109 target companies scanned for new roles every day.
- 370 roles found.
- 44 cut by the rules alone. 197 scored in full.
- 47 scored 8 or higher. Of the 115 roles the scanner found on its own, only 7 did. Most high scores were roles I picked by hand.
- 19 applications sent, each built from its evidence.

A strong role gets an application kit: the angle to lead with, the evidence, tailored resume bullets and a draft cover letter. Interview prep draws on the same evidence.

## How a non-developer built it

I am not a developer. AI agents wrote every line. My job was the control system around them.

Each piece of work had a written definition of done, a test for it, and a hand-off note for the next session. Two rules carried the load.

**Read the source before you change it.** Agents misquote code with confidence. Every task began by reading the real file. Every number on this page came from the production database.

**"Done" is a claim until someone checks it.** One agent reported a feature finished, tests passing. A line-by-line review found a hidden fault that would have broken outcome tracking in production, and four of six required tests switched off. The summary said done. The code did not.

## Why this matters for a revenue team

Define done, delegate the typing, verify the claims, keep the receipts.

I run revenue operations the same way: renewal scoring, capacity planning, go-to-market measurement. Here the team was AI agents. They were faster to hire.

---

For technical readers: the [build spec](build/searchops-rebuild.md), the [architecture decisions](adr/decisions.md), and the full scoring method in the app under Settings, Methodology.
