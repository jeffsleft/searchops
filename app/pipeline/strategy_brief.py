"""
Strategy brief builder. Append-only markdown document per company.
Sections are added, never rewritten.
"""
import json
from datetime import date

from app.models import get_db
from app.providers import get_provider
from app.scoring.prompts import STRATEGY_BRIEF_UPDATE_PROMPT, CHEATSHEET_PROMPT


def get_or_create_brief(job_id: int) -> dict:
    with get_db() as conn:
        job = conn.execute("SELECT company FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise ValueError(f"Job {job_id} not found")
        row = conn.execute("SELECT * FROM strategy_briefs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            initial = _build_initial_brief(job_id, job["company"], conn)
            conn.execute(
                "INSERT INTO strategy_briefs (job_id, company, content) VALUES (?,?,?)",
                (job_id, job["company"], initial),
            )
            row = conn.execute("SELECT * FROM strategy_briefs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row)


def _build_initial_brief(job_id: int, company: str, conn) -> str:
    _row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    job = dict(_row) if _row else None
    research_row = conn.execute(
        "SELECT result_json FROM research_cache WHERE company_name = ?", (company,)
    ).fetchone()
    research = json.loads(research_row["result_json"]) if research_row else {}

    lines = [
        f"# Strategy Brief: {company}",
        "",
        "## Company Overview",
        f"- Funding: {research.get('funding_stage', 'Unknown')}, {research.get('total_raised', 'Unknown')}",
        f"- Headcount: {research.get('headcount', 'Unknown')} ({research.get('headcount_trend', 'Unknown')})",
        f"- Revenue Model: {research.get('revenue_model', 'Unknown')}",
        f"- Pricing: {research.get('pricing_model', 'Unknown')}",
        f"- HQ: {research.get('hq_location', 'Unknown')}",
        f"- Competitors: {research.get('competitors', 'Unknown')}",
        f"- CEO: {research.get('ceo_founder_type', 'Unknown')}",
        "",
        "## Role Analysis",
        f"- Title: {job.get('job_title') if job else 'Unknown'}",
        f"- Score: {job.get('final_score')}/10" if job and job.get('final_score') else "- Score: TBD",
        f"- Greenfield: {job.get('greenfield') if job else 'Unknown'}",
        f"- Recommended Angle: {job.get('recommended_angle') if job else 'Unknown'}",
        "",
        "## Operational Debt Assessment",
        "### Process Debt",
        "- (to be updated from research and interviews)",
        "",
        "### Strategic Debt",
        "- (to be updated from research and interviews)",
        "",
        "## Metrics & Pricing Model",
        f"- Has FDE: {research.get('has_fde_model', 'Unknown')}",
        "- Sales metrics: Unknown",
        "- CS metrics: Unknown",
        "- Finance metrics: Unknown",
        "",
        "## Competitive Landscape",
        f"- {research.get('competitors', 'Unknown')}",
        "",
        "## Interview Intelligence",
        "",
        "## Jeff's Performance Scorecard",
        "",
        "## Recommendation",
        "_(generated after 2+ interviews)_",
    ]
    return "\n".join(lines)


def append_interview(
    job_id: int,
    contact_name: str,
    contact_title: str,
    interview_round: str,
    jeff_notes: str,
    transcript_analysis: dict | None = None,
) -> str:
    """Append new intelligence to the strategy brief. Returns updated content."""
    brief = get_or_create_brief(job_id)
    llm = get_provider()

    prompt = STRATEGY_BRIEF_UPDATE_PROMPT.format(
        company=brief.get("company", "Unknown"),
        current_brief=brief.get("content", ""),
        date=str(date.today()),
        contact_name=contact_name,
        contact_title=contact_title,
        round=interview_round,
        jeff_notes=jeff_notes,
        transcript_analysis=json.dumps(transcript_analysis, indent=2) if transcript_analysis else "Not available",
    )
    new_sections = llm.generate(prompt)

    updated_content = brief.get("content", "") + "\n\n" + new_sections
    with get_db() as conn:
        conn.execute(
            "UPDATE strategy_briefs SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
            (updated_content, job_id),
        )
    return updated_content


def generate_cheatsheet(job_id: int, interview_round: str, interviewers: str) -> str:
    """Generate a one-page interview cheat sheet."""
    from app.config import load_profile
    profile = load_profile()

    brief = get_or_create_brief(job_id)
    with get_db() as conn:
        _row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        job = dict(_row) if _row else {}
        high_qs = conn.execute(
            "SELECT question, persona_target, category FROM questions WHERE job_id = ? AND priority = 'High' AND status = 'unasked'",
            (job_id,),
        ).fetchall()
        research_row = conn.execute(
            "SELECT result_json FROM research_cache WHERE company_name = ?", (job["company"],)
        ).fetchone()

    research = json.loads(research_row["result_json"]) if research_row else {}
    interview_pref = profile.get("interview", {})
    anchor_stories = json.dumps([
        {"title": s.get("title", ""), "summary": s.get("summary", ""), "best_for": s.get("best_for", "")}
        for s in interview_pref.get("anchor_stories", [])
    ], indent=2)

    llm = get_provider()
    candidate = profile.get("candidate", {})
    prompt = CHEATSHEET_PROMPT.format(
        candidate_name=candidate.get("name", "Unknown"),
        company=job.get("company", "Unknown"),
        job_title=job.get("job_title", "Unknown"),
        research_summary=json.dumps(research, indent=2),
        strategy_brief=brief.get("content", "")[:3000],
        round=interview_round,
        interviewers=interviewers,
        anchor_stories=anchor_stories,
        high_priority_questions=json.dumps([dict(q) for q in high_qs], indent=2),
    )
    return llm.generate(prompt)
