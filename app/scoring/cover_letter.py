"""
WP-E — Full tailored cover letter.

Generates a complete cover letter (not just hooks) grounded in the candidate corpus,
the JD, prior match evidence, and any cached company research. Returns a structured
dict the .docx engine (app/resume_docx.build_cover_letter_docx) assembles into the
locked cover-letter template.

Never raises: on any failure (corpus missing, LLM error, bad JSON) returns a minimal
result so the caller can surface the failure rather than crash.
"""
from __future__ import annotations

import json
import logging

from app.providers import get_provider
from app.scoring.corpus import load_corpus, render_corpus_for_prompt
from app.scoring.prompts import COVER_LETTER_PROMPT
from app.scoring.research import _candidate_summary, get_cached_research
from app.scoring.schemas import CoverLetterResult
from app.config import load_profile


def _research_summary(company: str) -> str:
    """Compact, prompt-safe summary of cached research, or '' if none."""
    research = get_cached_research(company) if company else None
    if not research:
        return ""
    keys = [
        "funding_stage", "headcount", "revenue_model", "pricing_model",
        "customer_segments", "competitive_position", "outreach_hook",
        "timing_signal_rationale", "industry",
    ]
    lines = [f"{k.replace('_', ' ')}: {research[k]}"
             for k in keys if research.get(k) and research[k] != "Unknown"]
    return "\n".join(lines)


def _match_signal(job: dict) -> str:
    """Reuse the strongest match evidence + hooks already stored on the job."""
    parts: list[str] = []
    try:
        evidence = json.loads(job.get("match_evidence_json") or "[]")
        for e in evidence[:5]:
            if isinstance(e, dict):
                req = e.get("jd_requirement", "")
                acc = e.get("matched_accomplishment", "")
                if req and acc:
                    parts.append(f"- {req} -> {acc}")
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        hooks = json.loads(job.get("match_hooks_json") or "[]")
        for h in hooks[:3]:
            if isinstance(h, str) and h.strip():
                parts.append(f"- hook: {h}")
    except (json.JSONDecodeError, TypeError):
        pass
    if job.get("match_tailored_summary"):
        parts.append(f"- profile: {job['match_tailored_summary']}")
    return "\n".join(parts) if parts else "(no prior match signal stored)"


def generate_cover_letter(job: dict) -> dict:
    """Generate a full cover letter for a scored job row. Never raises.

    Returns a dict: {recipient, salutation, body[], closing, error?}.
    """
    company = job.get("company") or ""
    jd_text = job.get("jd_text") or ""

    corpus = load_corpus()
    if not corpus["available"]:
        return {
            "recipient": f"Hiring Team, {company}".strip(", "),
            "salutation": "Dear Hiring Team,",
            "body": [],
            "closing": "Sincerely,",
            "error": "Accomplishments corpus not available — cannot ground the letter.",
        }

    profile = load_profile()
    try:
        prompt = COVER_LETTER_PROMPT.format(
            candidate_summary=_candidate_summary(profile),
            corpus_text=render_corpus_for_prompt(corpus),
            jd_text=jd_text[:10000],
            company=company or "this company",
            research_summary=_research_summary(company) or "(no research available)",
            match_signal=_match_signal(job),
        )
        raw = get_provider().generate_json(prompt)
    except Exception as e:
        logging.error("generate_cover_letter LLM call failed for %s: %s", company, e)
        return {
            "recipient": f"Hiring Team, {company}".strip(", "),
            "salutation": "Dear Hiring Team,",
            "body": [],
            "closing": "Sincerely,",
            "error": f"{type(e).__name__}: {e}",
        }

    try:
        result = CoverLetterResult(**raw).dict()
    except Exception as e:
        logging.error("CoverLetterResult validation failed for %s: %s", company, e)
        result = {
            "recipient": raw.get("recipient", "") if isinstance(raw, dict) else "",
            "salutation": raw.get("salutation", "Dear Hiring Team,") if isinstance(raw, dict) else "Dear Hiring Team,",
            "body": raw.get("body", []) if isinstance(raw, dict) else [],
            "closing": raw.get("closing", "Sincerely,") if isinstance(raw, dict) else "Sincerely,",
        }
        if not isinstance(result["body"], list):
            result["body"] = []

    if not result.get("recipient"):
        result["recipient"] = f"Hiring Team, {company}".strip(", ")

    # WP-F de-AI voice pass. Lives here, not in the route, so every caller gets it —
    # the W2 kit assembles via the service layer and would otherwise ship an unpolished
    # letter while claiming "voice-passed". No-op (with a report) when disabled.
    from app.voice import polish_cover_letter

    if result.get("body"):
        result = polish_cover_letter(result)
    return result
