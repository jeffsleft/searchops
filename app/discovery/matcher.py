"""Lightweight fit analysis for discovered jobs using fast LLM."""
import json
import logging
from app.providers import get_provider
from app.scoring.schemas import DiscoveryFitAnalysis

logger = logging.getLogger(__name__)

# Condensed profile summary — derived from candidate_profile.yaml
CANDIDATE_SUMMARY = """
Name: Jeff Beaumont
Target roles: VP/Director of GTM Ops, RevOps, CS Ops, GTM Strategy
Target companies: 200-500 person SaaS, Series B/C
Strong-fit sectors: DevTools, AI-native SaaS, Fintech, PropTech, Construction Tech, Beverages/Logistics
Hard no sectors: Government, Healthcare, Gambling, Adult
Geography: Auburn CA base. Remote preferred. Hybrid Bay Area 1-2x/month OK.
Compensation floor: $120k base (auto-reject below)
Target: $200k-$300k base+bonus + equity
Work style preferences: Mac + Slack + Google Workspace. Strongly dislikes Windows + Teams.
Career edge: GitLab CS Ops leadership ($72M→$550M ARR), CPA + finance rigor, AI-native builder
Strong interest in: Greenfield builds (not just process upcycling), PLG/consumption pricing models, 2nd-time founders
"""


def _normalize_ops(s: str) -> str:
    """Normalize 'operations' → 'ops' so filter entries match both abbreviation and full word."""
    import re as _re
    return _re.sub(r'\boperations\b', 'ops', s)


def passes_title_filter(title: str, config: dict = None) -> bool:
    """Quick title check before spending LLM tokens."""
    import re
    title_lower = _normalize_ops(title.lower())

    # Use config-driven filters if available
    if config:
        positives = config.get("positive", [])
        negatives = config.get("negative", [])

        # Check negatives first (no normalization — "Operations Manager" shouldn't be blocked)
        for neg in negatives:
            if neg.lower() in title.lower():
                return False

        # Check positives against normalized title
        if positives:
            for pos in positives:
                if _normalize_ops(pos.lower()) in title_lower:
                    return True
            return False
            
    # Fallback to hardcoded defaults
    ops_terms = r'(gtm|revenue|revops|cs ops|customer success ops|sales ops|marketing ops|go.to.market|go to market)'
    seniority_terms = r'(vp|vice president|director|senior director|head of|principal)'
    strategy_terms = r'(strategy|operations|ops)'

    # Match: senior ops title
    if re.search(ops_terms, title_lower):
        return True
    # Match: VP/Director + strategy/operations
    if re.search(seniority_terms, title_lower) and re.search(strategy_terms, title_lower):
        return True
    return False


def generate_fit_analysis(title: str, description: str, company_name: str) -> dict:
    """Call fast LLM to generate fit bullets. Returns dict with fit_bullets, salary_mentioned, etc."""
    # Truncate description to control token cost
    desc_truncated = description[:2500] if len(description) > 2500 else description

    prompt = f"""You are evaluating job fit for a specific candidate. Return ONLY valid JSON.

CANDIDATE PROFILE:
---BEGIN UNTRUSTED INPUT---
{CANDIDATE_SUMMARY}
---END UNTRUSTED INPUT---

JOB TO EVALUATE:
Company: {company_name}
Title: {title}
Description:
---BEGIN UNTRUSTED INPUT---
{desc_truncated}
---END UNTRUSTED INPUT---

Return a JSON object with exactly these fields:
{{
  "fit_bullets": ["bullet 1", "bullet 2", "bullet 3"],
  "salary_mentioned": "range string or null",
  "greenfield_signal": true/false/null,
  "preliminary_score": 0.0-10.0
}}

Rules for fit_bullets:
- Exactly 3 bullets (4 if there's a strong specific signal worth noting)
- Each bullet must cite a SPECIFIC signal from the JD — not generic praise
- Examples of good bullets: "Mentions PLG motion and consumption pricing — strong match for Jeff's preferred model", "CS team is growing YoY per JD language — not a Churn & Burn signal", "Greenhouse listing suggests Mac/Slack-first tech culture"
- Examples of bad bullets: "This role seems like a good fit", "Matches your experience"
- If a bullet can't be specific, omit it and write fewer bullets
- salary_mentioned: extract range if visible in JD (e.g "$150k-$180k"), otherwise null
- greenfield_signal: true if JD says "build from scratch", "first in role", "standing up", "greenfield"; false if purely "optimize existing"; null if unclear
- preliminary_score: 0-10 score for how well this matches the candidate profile (10=dream job, 0=auto-reject)"""

    try:
        provider = get_provider()
        response = provider.generate(prompt, json_mode=True)
        data_raw = json.loads(response)
        
        try:
            data = DiscoveryFitAnalysis(**data_raw).dict()
        except Exception as e:
            logger.error(f"DiscoveryFitAnalysis validation failed for {title}: {e}")
            data = data_raw
            # Minimal cleanup
            if 'fit_bullets' not in data:
                data['fit_bullets'] = ["No specific fit signals identified"]
            if 'preliminary_score' not in data:
                data['preliminary_score'] = 5.0
        return data
    except Exception as e:
        logger.error(f"Fit analysis failed for {title}: {e}")
        return {
            'fit_bullets': ['Fit analysis unavailable'],
            'salary_mentioned': None,
            'greenfield_signal': None,
            'preliminary_score': 5.0
        }
