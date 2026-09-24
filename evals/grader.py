"""
evals/grader.py — provider-agnostic LLM-as-judge.

Scores output against a per-case rubric, with fabrication as the #1 thing to catch.
Judge provider resolves from: explicit arg → EVALS_JUDGE_PROVIDER → EVALS_PROVIDER →
"gemini". We enforce the pass threshold ourselves, not the model's own flag.
"""

from __future__ import annotations

import os

from providers import DEFAULT_MODELS, complete, extract_json

_GRADER_SYSTEM = """You are a strict evaluation judge.

Score the OUTPUT against the RUBRIC on a 1-5 integer scale:
  5 = fully meets the rubric
  3 = usable but with real gaps
  1 = badly fails

Be skeptical, not generous. The single most important failure to catch is FABRICATION:
any number, name, fact, or claim in the OUTPUT not supported by the INPUT CONTEXT must
lower the score sharply, even if the output reads well."""


def grade(
    *,
    output: str,
    rubric: str,
    context: str,
    threshold: int = 4,
    provider: str | None = None,
    model: str | None = None,
) -> dict:
    """Score one output. Returns {score:int, passed:bool, reasons:str}."""
    provider = provider or os.getenv("EVALS_JUDGE_PROVIDER") or os.getenv("EVALS_PROVIDER", "gemini")
    model = model or DEFAULT_MODELS[provider]
    user = (
        "INPUT CONTEXT (the only facts the output may rely on):\n"
        f"{context}\n\n"
        f"RUBRIC:\n{rubric}\n\n"
        f"OUTPUT TO GRADE:\n{output}\n\n"
        'Return ONLY a JSON object, no prose and no code fence:\n'
        '{"score": <integer 1-5>, "passed": <true|false>, "reasons": "<one or two sentences>"}'
    )
    text = complete(
        provider=provider,
        model=model,
        system=_GRADER_SYSTEM,
        user=user,
        max_tokens=1024,
        thinking=(provider == "anthropic"),
    )
    data = extract_json(text)
    score = int(data.get("score", 0))
    return {"score": score, "passed": score >= threshold, "reasons": str(data.get("reasons", ""))}
