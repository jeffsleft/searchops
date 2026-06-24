"""
Voice add-on (WP-F) — Weaver.

Optional LLM pass that rewrites a single block of prose to remove the phrasing
the Inspector flagged, without changing facts, names, numbers, or meaning. Pure
function of (text, violations, provider): never raises — on any failure it
returns the original text so cover-letter generation can never be broken by the
voice add-on.
"""
from __future__ import annotations

import logging

_REWRITE_PROMPT = """You are an editor removing the tells that make writing sound \
AI-generated. Rewrite the text below so it reads like a sharp, direct human wrote it.

HARD RULES:
- Do NOT invent or change any fact, name, number, company, date, or claim.
- Do NOT add new sentences or information. Keep the same length and meaning.
- Remove or replace these flagged words/phrases: {flagged}
- Keep it plain and specific. No corporate buzzwords, no throat-clearing, no \
filler transitions ("furthermore", "additionally", "moreover").
- Return ONLY the rewritten text — no preamble, no quotes, no explanation.

TEXT:
{text}"""


def rewrite(text: str, violations: list[dict], provider=None) -> str:
    """Rewrite ``text`` to drop the flagged terms. Returns original on any issue."""
    if not text or not violations:
        return text
    if provider is None:
        try:
            from app.providers import get_provider
            provider = get_provider()
        except Exception as e:  # provider misconfigured / unavailable
            logging.warning("voice.weaver: no provider available (%s); skipping rewrite", e)
            return text

    flagged = ", ".join(sorted({v["term"] for v in violations}))
    try:
        out = provider.generate(_REWRITE_PROMPT.format(flagged=flagged, text=text))
    except Exception as e:
        logging.warning("voice.weaver: rewrite call failed (%s); keeping original", e)
        return text

    out = (out or "").strip()
    # Guard against a model that returns nothing or balloons the text.
    if not out or len(out) > max(400, len(text) * 3):
        return text
    return out
