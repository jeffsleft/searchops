"""
Company research and signal-derivation business logic.
Extracted from routes.py to keep route handlers thin.
"""
import json
import logging
import time
from datetime import date

from app.models import get_db

GAP_HYPOTHESIS_PROMPT = """You are a CS Ops / GTM Ops hiring researcher.

Candidate profile:
{candidate_summary}

Target company: {company_name}
Category: {industry_category}
What they do: {what_they_do}

---BEGIN RESEARCH---
{research_json}
---END RESEARCH---

Using the above, produce a JSON object with three fields:

{{
  "gtm_motion": "<2-4 sentence description of their go-to-market motion: PLG/SLG/hybrid, direct/channel, SMB/MM/enterprise focus, how they sell>",
  "csrevops_setup": "<2-3 sentences on their CS and RevOps infrastructure: tools observed, team size signals, whether CS is separate from Sales, renewal ops maturity>",
  "gap_hypothesis": "<2-3 sentences on what operational gap Jeff Beaumont specifically fills for this company, grounded in the company research and Jeff's profile. Be specific — name the gap, name the signal. Example: 'Klarna's rapid US expansion suggests their RevOps motion is outpacing their operational infrastructure. Jeff's background scaling GitLab from $72M→$550M+ ARR and building AI-native CS Ops tooling maps directly to the infrastructure debt a company at this scale typically carries.'>"
}}

All three fields are required. Use the research if available; supplement with your knowledge of the company. Do not hallucinate funding figures or headcount — use hedged language if uncertain.
"""


def _map_work_arrangement(wa: str) -> str:
    """Normalize research work_arrangement value to the remote_friendly display format."""
    if not wa or wa.strip().lower() in ("unknown", ""):
        return ""
    w = wa.strip().lower()
    if w.startswith("remote"):
        return "Remote-first"
    if "hybrid" in w:
        return "Hybrid"
    if w.startswith("in-office") or w.startswith("in office"):
        return wa.strip()  # preserve location detail e.g. "in-office (SF, NYC)"
    return wa.strip()


def do_research_company(co_id: int, co: dict) -> None:
    """Run research + fit assessment for a company and persist results."""
    try:
        from app.scoring.research import research_company, assess_company_fit
        research = research_company(co["name"])
        fit = assess_company_fit(co["name"], research)
        research["fit_rationale"] = fit.get("fit_rationale")
        research["fit_justification"] = fit.get("fit_justification")
        research["need_rationale"] = fit.get("need_rationale")
        research["need_justification"] = fit.get("need_justification")

        remote_val = _map_work_arrangement(research.get("work_arrangement", ""))
        hq_val = research.get("hq_location", "") or ""
        if hq_val.strip().lower() in ("unknown", ""):
            hq_val = ""
        headcount_val = research.get("headcount", "") or ""
        if headcount_val.strip().lower() in ("unknown", ""):
            headcount_val = ""
        industry_val = research.get("industry", "") or ""
        if industry_val.strip().lower() in ("unknown", ""):
            industry_val = ""

        with get_db() as conn:
            conn.execute(
                """UPDATE companies SET research_json=?, research_date=?,
                   fit_score=?, need_assessment=?, funding_stage=?,
                   remote_friendly    = COALESCE(NULLIF(remote_friendly, ''), NULLIF(?, '')),
                   nearest_hq         = COALESCE(NULLIF(nearest_hq, ''), NULLIF(?, '')),
                   headcount_estimate = COALESCE(NULLIF(headcount_estimate, ''), NULLIF(?, '')),
                   industry_category  = COALESCE(NULLIF(industry_category, ''), NULLIF(?, '')),
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (json.dumps(research), str(date.today()),
                 fit.get("fit_score"), fit.get("need_assessment"),
                 research.get("funding_stage", ""),
                 remote_val, hq_val, headcount_val, industry_val,
                 co_id),
            )
    except Exception as e:
        logging.error("Research failed for company %s (%s): %s", co_id, co['name'], e)


def do_research_job(job_id: int, company_name: str) -> None:
    """Run company research for a job's target company, persist FDE/timing
    signals onto the job row, then refresh its strategy brief and question bank."""
    from app.scoring.research import research_company, assess_company_fit

    research = research_company(company_name, force=True)
    assess_company_fit(company_name, research)
    with get_db() as conn:
        conn.execute(
            """UPDATE jobs SET
               has_fde_model = ?,
               timing_signal = ?,
               timing_signal_rationale = ?
               WHERE id = ?""",
            (research.get("has_fde_model", "Unknown"),
             research.get("timing_signal", "Unknown"),
             research.get("timing_signal_rationale", ""),
             job_id)
        )
    from app.pipeline.strategy_brief import get_or_create_brief
    get_or_create_brief(job_id)
    from app.questions.bank import seed_questions
    seed_questions(job_id)


def generate_gap_hypothesis(co_id: int, force_research: bool = False, force_metadata: bool = False) -> dict:
    """
    Run the gap hypothesis pipeline for a Tier A company.
    Returns dict with gtm_motion, csrevops_setup, gap_hypothesis keys.
    Persists results to the companies row.
    Set force_research=True to bypass the research cache (e.g. when user clicks Research button).
    """
    from app.scoring.research import research_company, _candidate_summary
    from app.config import load_profile
    from app.providers import get_provider

    with get_db() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (co_id,)).fetchone()
        if not row:
            raise ValueError(f"Company {co_id} not found")
        co = dict(row)

    company_name = co["name"]
    industry_category = co.get("industry_category") or co.get("sector") or ""
    what_they_do = co.get("why_interesting") or ""

    # Re-use cached research unless force_research=True (e.g. user-triggered button)
    try:
        research = research_company(company_name, force=force_research)
    except Exception as e:
        logging.warning("Research fetch failed for %s, continuing with empty: %s", company_name, e)
        research = {}

    time.sleep(5)  # AI_RULES §1 pacing

    profile = load_profile()
    candidate_summary = _candidate_summary(profile)
    llm = get_provider()

    prompt = GAP_HYPOTHESIS_PROMPT.format(
        candidate_summary=candidate_summary,
        company_name=company_name,
        industry_category=industry_category,
        what_they_do=what_they_do,
        research_json=json.dumps(research, indent=2)[:8000],
    )

    try:
        result = llm.generate_json(prompt, web_search=True)
    except Exception as e:
        logging.error("Gap hypothesis LLM call failed for %s: %s", company_name, e)
        raise

    gtm_motion = str(result.get("gtm_motion") or "")[:2000]
    csrevops_setup = str(result.get("csrevops_setup") or "")[:2000]
    gap_hypothesis = str(result.get("gap_hypothesis") or "")[:2000]

    remote_val = _map_work_arrangement(research.get("work_arrangement", ""))
    hq_val = research.get("hq_location", "") or ""
    if hq_val.strip().lower() in ("unknown", ""):
        hq_val = ""
    headcount_val = research.get("headcount", "") or ""
    if headcount_val.strip().lower() in ("unknown", ""):
        headcount_val = ""
    funding_val = research.get("funding_stage", "") or ""
    if funding_val.strip().lower() == "unknown":
        funding_val = ""
    industry_val = research.get("industry", "") or ""
    if industry_val.strip().lower() in ("unknown", ""):
        industry_val = ""

    if force_metadata:
        meta_sql = """remote_friendly    = ?,
               nearest_hq         = ?,
               headcount_estimate = ?,
               funding_stage      = ?,
               industry_category  = ?,"""
    else:
        meta_sql = """remote_friendly    = COALESCE(NULLIF(remote_friendly, ''), NULLIF(?, '')),
               nearest_hq         = COALESCE(NULLIF(nearest_hq, ''), NULLIF(?, '')),
               headcount_estimate = COALESCE(NULLIF(headcount_estimate, ''), NULLIF(?, '')),
               funding_stage      = COALESCE(NULLIF(funding_stage, ''), NULLIF(?, '')),
               industry_category  = COALESCE(NULLIF(industry_category, ''), NULLIF(?, '')),"""

    with get_db() as conn:
        conn.execute(
            f"""UPDATE companies SET
               gtm_motion = ?,
               csrevops_setup = ?,
               gap_hypothesis = ?,
               gap_hypothesis_date = ?,
               {meta_sql}
               updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (gtm_motion, csrevops_setup, gap_hypothesis, str(date.today()),
             remote_val, hq_val, headcount_val, funding_val, industry_val, co_id),
        )

    return {
        "gtm_motion": gtm_motion,
        "csrevops_setup": csrevops_setup,
        "gap_hypothesis": gap_hypothesis,
    }


def derive_signals(research: dict) -> list[dict]:
    """Build a signals array from research fields for the company panel UI."""
    if isinstance(research.get("signals"), list) and research["signals"]:
        return research["signals"]

    signals: list[dict] = []
    red_flags = (research.get("red_flags") or "").strip()
    if red_flags and red_flags.lower() not in ("none detected", "unknown", "none"):
        signals.append({"severity": "warn", "text": red_flags})

    if research.get("cs_shrinking_sales_growing") is True:
        signals.append({"severity": "warn", "text": "CS team shrinking while Sales grows — Churn & Burn pattern."})

    if research.get("is_greenfield") is True:
        signals.append({"severity": "good", "text": "Greenfield opportunity — building, not maintaining."})

    if research.get("has_fde_model") == "Yes":
        signals.append({"severity": "good", "text": "Operates an FDE model — strong fit for systems-led GTM."})

    gd = (research.get("glassdoor_sentiment") or "").strip()
    if gd and gd.lower() != "unknown":
        sev = "warn" if gd.lower().startswith("negative") else ("good" if gd.lower().startswith("positive") else "info")
        signals.append({"severity": sev, "text": f"Glassdoor: {gd}"})

    return signals
