"""
Layer 2 — Match to Candidate.

Compares a JD against the candidate's full accomplishments corpus via one LLM call
and returns: match_score (-3.0 to +3.0), match_summary, evidence[], mismatches[],
differentiator_themes[], tailored_bullets[], cover_letter_hooks[].

If the corpus is unavailable (docx not yet delivered), returns a zero-contribution
record so the rest of the scoring pipeline still runs.
"""
from __future__ import annotations
import logging

from app.providers import get_provider
from app.scoring.corpus import load_corpus, render_corpus_for_prompt
from app.scoring.prompts import MATCH_PROMPT
from app.scoring.schemas import MatchResult


EMPTY_RESULT = {
    "match_score": 0.0,
    "match_summary": "",
    "evidence": [],
    "mismatches": [],
    "differentiator_themes": [],
    "tailored_summary": "",
    "tailored_bullets": [],
    "cover_letter_hooks": [],
    "sections_to_drop": [],
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_match(jd_text: str, candidate_summary: str) -> dict:
    """
    Run Layer 2. Returns the dict above; never raises.

    On any failure (corpus missing, LLM error, bad JSON) returns EMPTY_RESULT with a
    populated `mismatches` entry explaining the failure mode, so the UI surfaces it
    rather than silently zeroing.
    """
    corpus = load_corpus()
    if not corpus["available"]:
        out = dict(EMPTY_RESULT)
        out["mismatches"] = [{
            "jd_requirement": "(corpus not available)",
            "gap": f"Accomplishments Inventory not found at {corpus['path']}. "
                   "Layer 2 will contribute 0.0 until the docx is delivered.",
            "severity": "High",
        }]
        return out

    corpus_text = render_corpus_for_prompt(corpus)

    try:
        llm = get_provider()
        prompt = MATCH_PROMPT.format(
            candidate_summary=candidate_summary,
            corpus_text=corpus_text,
            jd_text=jd_text[:10000],
        )
        result_raw = llm.generate_json(prompt)
    except Exception as e:
        out = dict(EMPTY_RESULT)
        out["mismatches"] = [{
            "jd_requirement": "(layer 2 call failed)",
            "gap": f"LLM call raised: {type(e).__name__}: {e}",
            "severity": "High",
        }]
        return out

    try:
        result = MatchResult(**result_raw).dict()
    except Exception as e:
        logging.error(f"MatchResult validation failed: {e}")
        # Manual fallback normalization if Pydantic fails
        result = result_raw
        try:
            score = float(result.get("match_score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        result["match_score"] = round(_clamp(score, -4.0, 4.0), 2)

        for key in ("evidence", "mismatches", "differentiator_themes",
                    "tailored_bullets", "cover_letter_hooks"):
            if not isinstance(result.get(key), list):
                result[key] = []

        if not isinstance(result.get("match_summary"), str):
            result["match_summary"] = ""

    return result
