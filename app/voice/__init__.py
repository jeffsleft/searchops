"""
Voice add-on (WP-F) — public API.

Optional, pluggable de-AI pass for generated prose. Ships a working built-in
guide; point ``VOICE_GUIDE_PATH`` at your own forbidden-words YAML to override,
or set ``VOICE_ENABLED=0`` to turn it off (generation still works either way).

Pipeline: Inspector (deterministic scan) → Weaver (optional LLM rewrite) →
re-scan. The whole module never raises into its callers — worst case the prose
passes through unchanged.

Typical use::

    from app.voice import polish_cover_letter, is_enabled
    cl = polish_cover_letter(cl)        # adds cl["voice"] report; no-op if disabled
"""
from __future__ import annotations

from app import config
from app.voice import inspector, weaver

__all__ = [
    "is_enabled", "active_guide_label", "scan_text", "polish_text", "polish_cover_letter",
]


def is_enabled() -> bool:
    return bool(config.VOICE_ENABLED)


def _guide(tone: str) -> dict:
    return inspector.load_guide(tone=tone, user_path=config.VOICE_GUIDE_PATH)


def active_guide_label() -> str:
    """Human-readable name of the guide that would be applied (for the UI)."""
    return _guide("cover-letter")["label"]


def scan_text(text: str, tone: str = "cover-letter") -> list[dict]:
    """Deterministic scan only — list of violations. Always available."""
    return inspector.scan(text, _guide(tone))


def polish_text(text: str, tone: str = "cover-letter", provider=None) -> tuple[str, dict]:
    """Scan → (rewrite if flagged) → re-scan one block of prose.

    Returns ``(new_text, report)`` where report =
    ``{enabled, guide, before, after, rewritten}``.
    """
    if not is_enabled():
        return text, {"enabled": False, "guide": None, "before": 0, "after": 0, "rewritten": False}

    guide = _guide(tone)
    before = inspector.scan(text, guide)
    if not before:
        return text, {"enabled": True, "guide": guide["label"], "before": 0, "after": 0, "rewritten": False}

    new_text = weaver.rewrite(text, before, provider=provider)
    after = inspector.scan(new_text, guide)
    return new_text, {
        "enabled": True,
        "guide": guide["label"],
        "before": len(before),
        "after": len(after),
        "rewritten": new_text != text,
    }


def polish_cover_letter(cl: dict, provider=None) -> dict:
    """Polish a cover-letter dict's body paragraphs + closing in place.

    Attaches ``cl["voice"]`` with an aggregate report. No-op (with a report)
    when the add-on is disabled. Never raises.
    """
    if not isinstance(cl, dict):
        return cl
    if not is_enabled():
        cl["voice"] = {"enabled": False, "guide": None, "before": 0, "after": 0, "rewritten": False}
        return cl

    total_before = total_after = 0
    rewritten = False

    body = cl.get("body")
    if isinstance(body, list):
        new_body: list[str] = []
        for para in body:
            if not isinstance(para, str) or not para.strip():
                new_body.append(para)
                continue
            new_para, rep = polish_text(para, tone="cover-letter", provider=provider)
            new_body.append(new_para)
            total_before += rep["before"]
            total_after += rep["after"]
            rewritten = rewritten or rep["rewritten"]
        cl["body"] = new_body

    closing = cl.get("closing")
    if isinstance(closing, str) and closing.strip():
        new_closing, rep = polish_text(closing, tone="cover-letter", provider=provider)
        cl["closing"] = new_closing
        total_before += rep["before"]
        total_after += rep["after"]
        rewritten = rewritten or rep["rewritten"]

    cl["voice"] = {
        "enabled": True,
        "guide": active_guide_label(),
        "before": total_before,
        "after": total_after,
        "rewritten": rewritten,
    }
    return cl
