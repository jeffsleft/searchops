# Job Discovery Engine — Technical Specification

## Overview

The Job Discovery Engine (Phase 0) is an automated layer that discovers matching job postings at monitored target companies. Instead of waiting for Jeff to manually identify roles, the system scans company ATS portals on a schedule and surfaces roles that match specific criteria.

**Key principle:** Discovered jobs enter a new pre-identified "discovered" stage, where they can be promoted to "identified" (and scored) or dismissed.

---

## Architecture

```
┌─────────────────────────────────────────┐
│  Modal Scheduler (6am UTC daily)         │
└──────────────┬──────────────────────────┘
               │
         run_discovery_scan()
               │
        ┌──────▼──────┐
        │ Hunter      │
        │ (orchestr.) │
        └──────┬──────┘
               │
   ┌───────────┼───────────┐
   │           │           │
   ▼           ▼           ▼
 fetch_     fetch_      fetch_
 greenhouse lever       ashby
   │           │           │
   └───────────┼───────────┘
               │
        ┌──────▼───────────┐
        │ Matcher          │
        │ (LLM + filters)  │
        └──────┬───────────┘
               │
        ┌──────▼──────────────┐
        │ Insert to jobs DB   │
        │ stage='discovered'  │
        └─────────────────────┘
```

---

## Components

### 1. ATS Clients (`app/discovery/ats_clients.py`)

Public REST/GraphQL APIs for three major ATS platforms. No authentication required.

**detect_ats(careers_url: str) -> (ats_type, ats_handle)**
- Regex-match the careers URL to identify ATS type and handle
- Patterns:
  - Greenhouse: `greenhouse.io/boards/{handle}` or `greenhouse.io/{handle}`
  - Lever: `lever.co/{handle}`
  - Ashby: `ashbyhq.com/{handle}`
- Returns: `('greenhouse'|'lever'|'ashby'|'playwright', handle_string)`
- Playwright fallback returns `('playwright', '')` for unknown ATS (not yet implemented)

**Greenhouse API**
- Endpoint: `https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true`
- Returns all public job postings with full descriptions
- Parse fields: `title`, `absolute_url`, `content` (description), `updated_at`, `location.name`

**Lever API**
- Endpoint: `https://api.lever.co/v0/postings/{handle}?mode=json`
- Returns postings array with nested description structure
- Parse fields: `text` (title), `hostedUrl`, `descriptionBody.body[].text` (description)

**Ashby GraphQL**
- Endpoint: `https://jobs.ashbyhq.com/api/non-user-graphql`
- Query: `ApiJobBoardWithTeams` — fetch all job postings for org
- Parse fields: `jobPostings[].title`, `.id`, `.isListed`, `.locationName`, `.jobRequisition.description`
- Filter: only include `isListed: true` postings

**Error handling:**
- All clients log errors and return empty list on failure
- HTTP timeout: 15 seconds per request
- Caller decides retry strategy

---

### 2. Matcher (`app/discovery/matcher.py`)

Fast, LLM-powered fit analysis to avoid wasting time on non-matches.

**passes_title_filter(title: str) -> bool**
- Regex check: does title contain ops/gtm/revops/strategy keywords?
- Returns True if title is worth analyzing (saves LLM tokens)
- Examples that pass:
  - "VP of Revenue Operations"
  - "Director of GTM"
  - "Senior Sales Operations Manager"
- Examples that fail:
  - "Software Engineer"
  - "Sales Representative"
  - "Marketing Manager"

**generate_fit_analysis(title, description, company_name) -> dict**
- Call Gemini (via get_provider()) with `json_mode=True`
- Input prompt includes:
  - CANDIDATE_SUMMARY (Jeff's profile, sector preferences, comp floor, etc.)
  - Job title, company, description (truncated to 2500 chars)
- Output: JSON object with:
  ```json
  {
    "fit_bullets": ["specific signal 1", "specific signal 2", "specific signal 3"],
    "preliminary_score": 0.0-10.0,
    "salary_mentioned": "range or null",
    "greenfield_signal": true/false/null
  }
  ```
- **fit_bullets rules:**
  - MUST cite specific signals from the JD, not generic praise
  - Examples of good bullets:
    - "JD mentions PLG motion — strong alignment with Jeff's preferred model"
    - "CS team growing YoY per description — rules out Churn & Burn signal"
  - Examples of bad bullets:
    - "This role seems like a good fit"
    - "Matches your experience"
  - If fewer than 3 bullets can be specific, return 1-2 bullets
- Fallback: if LLM fails, return sensible defaults (score 5.0, generic bullet)

---

### 3. Hunter (`app/discovery/hunter.py`)

Orchestrator that runs the full discovery scan.

**run_discovery_scan() -> dict{scanned: int, new_found: int, errors: int}**
1. Query all companies WHERE `hunt_enabled = 1`
2. For each company:
   - Call `fetch_jobs_for_company(ats_type, ats_handle, careers_url)`
   - For each job returned:
     - Check title with `passes_title_filter()`
     - Dedup by URL (skip if already in jobs table)
     - Call `generate_fit_analysis()`
     - INSERT to jobs table with `pipeline_stage='discovered'`, `discovery_source='hunter'`, fit_bullets JSON, lightweight_score
   - Update `companies.last_scanned` and `companies.scan_error` after attempt
3. If any new jobs found, call `send_discovery_notification()`
4. Log stats and return summary

**Dedup strategy:**
- Before inserting, query: `SELECT id FROM jobs WHERE url = ?`
- Skip if match found (assume already in system)

**Database insert:**
```sql
INSERT INTO jobs (
  company, job_title, url, pipeline_stage, discovery_source,
  fit_bullets, lightweight_score, date_added
) VALUES (?, ?, ?, 'discovered', 'hunter', ?, ?, ?)
```

---

### 4. Routes

**GET /discovered**
- Render discovered.html with jobs WHERE `pipeline_stage='discovered'` ordered by date_added DESC
- Show fit_bullets as bullet list, preliminary_score as lightweight indicator
- Action buttons: Promote, Dismiss

**POST /job/{job_id}/promote**
- Move job from discovered → identified
- Trigger scoring (fetch JD, call score_job, update final_score)
- If score ≥ HIGH_SCORE_THRESHOLD, send Slack alert
- Redirect to job detail

**POST /job/{job_id}/dismiss**
- Move job to i_declined with decline_reason='dismissed_from_discovery'
- Redirect to /discovered

**GET /targets**
- List all companies WHERE `hunt_enabled=1`
- Show: name, ats_type badge, last_scanned, scan_error
- Form: + Add Target (name + careers_url)
- Actions per company: Scan Now, Unmonitor toggle

**POST /targets/add**
- Accept form: name, careers_url
- Auto-detect ATS via detect_ats()
- Upsert company with hunt_enabled=1
- Redirect to /targets

**POST /targets/{co_id}/toggle**
- Flip hunt_enabled 0↔1
- Redirect to /targets

**POST /targets/{co_id}/scan-now**
- Immediate discovery scan for one company (sync)
- Return JSON: {scanned, new_found, errors}
- UI shows results inline

**POST /api/discovery/scan**
- Trigger full discovery_scan() synchronously
- Return JSON: {scanned, new_found, errors}
- Used by dashboard "Run Scan Now" button

---

### 5. Slack Notifications

**send_discovery_notification(new_jobs: list[dict]) -> bool**
- Posts to SLACK_WEBHOOK_URL
- Format:
  ```
  :mag: *Job Discovery Scan — N New Role(s)*
  
  • Company A — Role Title
  • Company B — Role Title
  
  [View in Recruiting Engine]
  ```
- Called only if new_jobs is non-empty

---

### 6. Scheduler Integration

**app/main.py scheduler()**
- Runs at 0, 6, 12, 18 UTC (Modal cron)
- At 6am UTC: call `run_discovery_scan()`
- Also: process Google Sheets (every run), send weekly digest (Monday 8am PT)

---

## Data Model

### Companies table (new columns)
```sql
hunt_enabled INTEGER DEFAULT 0      -- Is this company monitored?
careers_url TEXT                     -- Base URL for ATS portal
ats_type TEXT                        -- 'greenhouse' | 'lever' | 'ashby' | 'playwright'
ats_handle TEXT                      -- Company slug/handle for API
last_scanned TEXT (ISO 8601)        -- Last successful scan timestamp
scan_error TEXT                      -- Error message from last scan, if any
```

### Jobs table (new columns)
```sql
discovery_source TEXT DEFAULT 'manual'  -- 'manual' | 'hunter' | 'sheet'
fit_bullets TEXT (JSON array)           -- ["bullet 1", "bullet 2", "bullet 3"]
lightweight_score REAL                  -- 0-10 preliminary score from LLM
date_added TEXT (ISO 8601)             -- When job was added to system
```

### Pipeline stages
```python
STAGES = {
    "discovered":    {"label": "Discovered",    "terminal": False},
    "identified":    {"label": "Identified",    "terminal": False},
    ...
}
```

---

## Filtering & Dedup Strategy

### Title Filter (before LLM)
- Regex patterns for ops/gtm/revops/strategy keywords
- Saves ~90% of LLM token cost by skipping irrelevant roles
- Examples:
  - ✅ "VP of Revenue Operations"
  - ✅ "Director, GTM Strategy"
  - ❌ "Account Executive"
  - ❌ "Support Engineer"

### URL Dedup
- Query existing jobs by URL before INSERT
- Prevents duplicate discovery entries
- Handles API returning same job multiple times

### Manual Review
- Every discovered job must be manually promoted or dismissed
- No auto-scoring on discovery (lightweight_score is just a hint)
- Promoted jobs go through full scoring pipeline

---

## Future Enhancements (Phase 1+)

- **Playwright fallback:** For ATS systems not covered by public APIs, use headless browser to scrape job listings
- **Custom careers pages:** Detect generic careers.html URLs and extract job links
- **Email alerts:** Send daily digest of high-fit discoveries (lightweight_score > 7)
- **Sector filtering:** Add hunt_enabled_sectors to companies, skip mismatched roles
- **Greenfield detection:** Pre-flag jobs with "build from scratch" or "0→1" language
- **Price signal detection:** Extract salary range from JD description if not in metadata

---

## Troubleshooting

**API Timeout / Connection Error:**
- Check ATS service status (Greenhouse, Lever, Ashby)
- Verify careers_url and ats_handle are correct
- Manually test endpoint in browser

**No jobs found for monitored company:**
- Check if JDs are marked as "published" in their ATS
- Verify title filter isn't too restrictive
- Run manual Playwright scan if using fallback

**Fit analysis missing bullets:**
- Check Gemini API quota (may be rate-limited)
- Verify JD description is being passed correctly
- Check logs for LLM error messages

---

## Testing

Example discovery flow:
1. Add Harness (Greenhouse) to hunt targets: `https://boards.greenhouse.io/harness`
2. Run immediate scan: POST /targets/{co_id}/scan-now
3. Check discovered.html — should show matched roles
4. Promote one: POST /job/{job_id}/promote
5. Verify moved to identified, scored, and added to pipeline

---

## References

- Greenhouse API: https://developers.greenhouse.io/job-board.html
- Lever API: https://docs.lever.co/
- Ashby GraphQL: https://developer.ashbyhq.com/
