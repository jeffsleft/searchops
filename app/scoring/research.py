"""
Company research agent. Calls Gemini with web search grounding.
Results are cached in SQLite for RESEARCH_CACHE_TTL_DAYS.
"""
import json
import logging
from datetime import datetime, timedelta

from app.config import load_profile, RESEARCH_CACHE_TTL_DAYS
from app.models import get_db
from app.providers import get_provider
from app.scoring.prompts import (
    RESEARCH_PROMPT, COMPANY_FIT_PROMPT, SCORING_PROMPT,
    INTERVIEWER_RESEARCH_PROMPT, INTERVIEW_COACHING_PROMPT,
)
from app.scoring.engine import (
    check_auto_reject,
    calculate_adjustment_weights,
    compute_final_score,
)
from app.scoring.match import score_match, EMPTY_RESULT as EMPTY_MATCH
from app.scoring.schemas import ScoringResult, ResearchResult, CompanyFitResult, InterviewerResearchResult, InterviewCoachingResult

MAX_TRANSCRIPT_CHARS = 15000

_METADATA_PROMPT = """Extract the company name and job title from this job description.
Return JSON with exactly two keys: {{"company": "<name>", "job_title": "<title>"}}.
If either cannot be determined, use "Unknown".
---BEGIN JD---
{jd_text}
---END JD---"""


def _extract_job_metadata(jd_text: str) -> dict:
    """Quick LLM call to extract company + title. Used for auto-rejected jobs that
    skip the full SCORING_PROMPT. Returns dict with 'company' and 'job_title' keys."""
    try:
        llm = get_provider()
        result = llm.generate_json(_METADATA_PROMPT.format(jd_text=jd_text[:3000]))
        return {
            "company": result.get("company") or "Unknown",
            "job_title": result.get("job_title") or "Unknown",
        }
    except Exception:
        return {"company": "Unknown", "job_title": "Unknown"}

def _candidate_summary(profile: dict) -> str:
    c = profile.get("candidate", {})
    name = c.get("name", "Unknown")
    summary = c.get("summary", "").strip()
    identity = c.get("identity", {})
    preferred_titles = identity.get("preferred_titles", [])
    titles_str = ", ".join(preferred_titles[:4]) if preferred_titles else "(not specified)"
    comp = profile.get("compensation", {})
    base_min = comp.get("base_min", 0)
    location = c.get("location", "Unknown")
    
    return (
        f"{name} — {summary}\n"
        f"Target roles: {titles_str}\n"
        f"Compensation floor: ${base_min:,}\n"
        f"Location: {location}, remote preferred"
    )


def get_cached_research(company_name: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT result_json, cached_at FROM research_cache WHERE company_name = ?",
            (company_name,),
        ).fetchone()
        if not row:
            return None
        cached_at = datetime.fromisoformat(row["cached_at"])
        if datetime.now() - cached_at > timedelta(days=RESEARCH_CACHE_TTL_DAYS):
            return None
        return json.loads(row["result_json"])


def save_research_cache(company_name: str, result: dict) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO research_cache (company_name, result_json, cached_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(company_name) DO UPDATE SET
                 result_json = excluded.result_json,
                 cached_at = excluded.cached_at""",
            (company_name, json.dumps(result)),
        )


def research_company(company_name: str, force: bool = False) -> dict:
    """Run Gemini research on a company. Returns parsed research dict."""
    if not force:
        cached = get_cached_research(company_name)
        if cached:
            return cached

    llm = get_provider()
    prompt = RESEARCH_PROMPT.format(company_name=company_name)
    result_raw = llm.generate_json(prompt, web_search=True)
    
    try:
        result = ResearchResult(**result_raw).dict()
    except Exception as e:
        logging.error(f"ResearchResult validation failed for {company_name}: {e}")
        result = result_raw

    save_research_cache(company_name, result)
    return result


def assess_company_fit(company_name: str, research: dict | None = None) -> dict:
    """Assess fit and need for a company — used for proactive (no-JD) workflow."""
    if research is None:
        research = research_company(company_name)

    profile = load_profile()
    llm = get_provider()
    prompt = COMPANY_FIT_PROMPT.format(
        candidate_summary=_candidate_summary(profile),
        research_json=json.dumps(research, indent=2),
    )
    result_raw = llm.generate_json(prompt)
    
    try:
        return CompanyFitResult(**result_raw).dict()
    except Exception as e:
        logging.error(f"CompanyFitResult validation failed for {company_name}: {e}")
        return result_raw


def research_interviewer(name: str, title: str, company: str) -> dict:
    """Research an interviewer before a meeting. Not cached (person-specific)."""
    llm = get_provider()
    prompt = INTERVIEWER_RESEARCH_PROMPT.format(
        interviewer_name=name,
        interviewer_title=title,
        company=company,
    )
    result_raw = llm.generate_json(prompt, web_search=True)
    
    try:
        return InterviewerResearchResult(**result_raw).dict()
    except Exception as e:
        logging.error(f"InterviewerResearchResult validation failed for {name}: {e}")
        return result_raw


def coach_interview(
    transcript_text: str,
    company: str,
    job_title: str,
    interviewer: str,
    interview_date: str,
) -> dict:
    """Score Jeff's interview performance from a transcript."""
    llm = get_provider()
    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        logging.warning(f"Transcript truncated from {len(transcript_text)} to {MAX_TRANSCRIPT_CHARS} chars")
        transcript_text = transcript_text[:MAX_TRANSCRIPT_CHARS]
        
    prompt = INTERVIEW_COACHING_PROMPT.format(
        company=company,
        job_title=job_title,
        interviewer=interviewer,
        date=interview_date,
        transcript_text=transcript_text,
    )
    result_raw = llm.generate_json(prompt)
    
    try:
        return InterviewCoachingResult(**result_raw).dict()
    except Exception as e:
        logging.error(f"InterviewCoachingResult validation failed: {e}")
        return result_raw


def score_job(jd_text: str, company_info: dict | None = None) -> dict:
    """
    Four-layer scoring pipeline:
      1. Auto-Reject — blocked sectors, salary floor.
      2. Match to Candidate — corpus-grounded LLM call (highest weight, -3.0..+3.0).
      3. LLM Qualitative Adjustment — role-shape vibe (-1.0..+1.0).
      4. Adjustment Weights — deterministic substring signals (additive).
    Final = clamp_0_10(base + L4 + L2 + L3). Single end-clamp.

    Returns {"jd_insufficient": True} if the content lacks enough role-specific
    detail — callers should mark the row as unscored rather than writing noise.
    """
    # Belt-and-suspenders: catch obviously empty/minimal pages before spending tokens.
    MIN_JD_LENGTH = 300
    if not jd_text or len(jd_text.strip()) < MIN_JD_LENGTH:
        return {"jd_insufficient": True}

    profile = load_profile()
    ci = company_info or {}
    candidate_summary = _candidate_summary(profile)

    rejected, reason = check_auto_reject(jd_text, profile)
    adjustment_result = calculate_adjustment_weights(jd_text, ci)

    if rejected:
        metadata = _extract_job_metadata(jd_text)
        return compute_final_score(
            profile=profile,
            auto_reject=(True, reason),
            adjustment_result=adjustment_result,
            match_result=dict(EMPTY_MATCH),
            llm_result=metadata,
        )

    # Layer 2 — corpus match (gracefully no-ops if Inventory docx not present)
    match_result = score_match(jd_text, candidate_summary)

    # Layer 3 — qualitative LLM nudge (role shape)
    llm = get_provider()
    prompt = SCORING_PROMPT.format(
        candidate_summary=candidate_summary,
        jd_text=jd_text[:10000],
        deterministic_score=round(
            profile.get("scoring", {}).get("base_score", 5.0) + adjustment_result.get("adjustment_score", 0.0), 1
        ),
        flags=", ".join(adjustment_result.get("flags", [])) or "none",
    )
    llm_result_raw = llm.generate_json(prompt)
    
    if llm_result_raw.get("jd_insufficient"):
        return {"jd_insufficient": True}

    try:
        llm_result = ScoringResult(**llm_result_raw).dict()
    except Exception as e:
        logging.error(f"ScoringResult validation failed: {e}")
        llm_result = llm_result_raw

    return compute_final_score(
        profile=profile,
        auto_reject=(False, None),
        adjustment_result=adjustment_result,
        match_result=match_result,
        llm_result=llm_result,
    )
