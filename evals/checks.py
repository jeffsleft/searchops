"""Deterministic checks for Layer 2 (match) output. Free, no API.

The #1 failure to catch is fabrication: evidence or metrics the candidate's
corpus doesn't contain. These checks are strict proxies for that; the LLM
judge (grader.py) covers what a word match can't.
"""
import re

_NONE = "(none found in corpus)"
_STOP = {"with", "from", "that", "this", "their", "have", "into", "over", "across",
         "while", "which", "through", "including", "within", "team", "teams"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3 and w not in _STOP}


def _numbers(text: str) -> set[str]:
    # 68%, $50M, 2.5x, 1,200 -> normalized digit strings
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def valid_shape(result: dict, ctx: dict):
    score = result.get("match_score")
    ok = isinstance(score, (int, float)) and -4.0 <= score <= 4.0 and isinstance(result.get("evidence"), list)
    return ok, f"match_score={score!r}, evidence rows={len(result.get('evidence') or [])}"


def score_in_range(result: dict, ctx: dict):
    lo, hi = ctx["case"]["match_score_range"]
    s = result.get("match_score", 0.0)
    return lo <= s <= hi, f"{s} expected in [{lo}, {hi}]"


def evidence_grounded(result: dict, ctx: dict, min_overlap: float = 0.6):
    """Each cited accomplishment must share most of its content words with the corpus."""
    corpus = _words(ctx["corpus_text"])
    bad = []
    for row in result.get("evidence") or []:
        cited = (row.get("matched_accomplishment") or "").strip()
        if not cited or cited == _NONE:
            continue
        words = _words(cited)
        if words and len(words & corpus) / len(words) < min_overlap:
            bad.append(cited[:80])
    return not bad, f"{len(bad)} evidence row(s) not traceable to the corpus: {bad[:3]}"


def metrics_grounded(result: dict, ctx: dict):
    """Every number in generated resume/cover-letter text must appear in the corpus or JD."""
    allowed = _numbers(ctx["corpus_text"]) | _numbers(ctx["jd_text"])
    texts = [result.get("tailored_summary") or ""]
    texts += [b.get("bullet", "") if isinstance(b, dict) else str(b) for b in result.get("tailored_bullets") or []]
    texts += [str(h) for h in result.get("cover_letter_hooks") or []]
    invented = sorted({n for t in texts for n in _numbers(t)} - allowed)
    return not invented, f"numbers not in corpus or JD: {invented[:8]}"


CHECKS = [valid_shape, score_in_range, evidence_grounded, metrics_grounded]
