"""
Voice add-on (WP-F) — Inspector.

Deterministic scan of prose against a forbidden-words guide. No LLM, no network:
this is the part that always runs and the part the tests pin. The Weaver
(weaver.py) is the optional LLM pass that acts on what the Inspector finds.

A guide is a list of rules, each either a ``phrase`` (case-insensitive substring)
or a ``word`` (whole-word, word-boundary match), with a ``reason`` and an optional
``per_piece_limit`` (only a violation once occurrences exceed the limit).
"""
from __future__ import annotations

import re
import functools
from pathlib import Path

import yaml

_CONSTRAINTS_DIR = Path(__file__).parent / "constraints"
BUILTIN_LABEL = "Built-in (don't-sound-like-AI)"


def _normalize_rules(raw: dict) -> list[dict]:
    """Accept either `forbidden:` (base) or `additional_forbidden:` (overlay)."""
    if not isinstance(raw, dict):
        return []
    rules = raw.get("forbidden") or raw.get("additional_forbidden") or []
    out: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        term = r.get("phrase") or r.get("word")
        if not term:
            continue
        out.append({
            "term": str(term),
            "is_word": "word" in r,
            "reason": r.get("reason", "ai-tell"),
            "limit": int(r.get("per_piece_limit", 0) or 0),
        })
    return out


@functools.lru_cache(maxsize=8)
def _load_yaml(path_str: str) -> tuple:
    """Parse a guide YAML into a tuple of rule-dicts (cached, hashable key)."""
    try:
        with open(path_str, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return tuple()
    return tuple(tuple(sorted(rule.items())) for rule in _normalize_rules(raw))


def _rules_from(path: Path) -> list[dict]:
    return [dict(t) for t in _load_yaml(str(path))]


def load_guide(tone: str = "cover-letter", user_path: str = "") -> dict:
    """Build the active guide.

    If ``user_path`` is set and readable, it replaces the built-in base.
    Otherwise the built-in base + the tone overlay (if any) are merged.
    Returns ``{"label": str, "rules": list[dict]}``.
    """
    if user_path and Path(user_path).is_file():
        rules = _rules_from(Path(user_path))
        if rules:
            return {"label": f"Custom ({Path(user_path).name})", "rules": rules}

    rules = _rules_from(_CONSTRAINTS_DIR / "forbidden_base.yaml")
    overlay = _CONSTRAINTS_DIR / f"{tone.replace('-', '_')}.yaml"
    if overlay.is_file():
        rules = rules + _rules_from(overlay)
    return {"label": BUILTIN_LABEL, "rules": rules}


def _count(term: str, is_word: bool, text: str) -> int:
    if is_word:
        return len(re.findall(r"\b" + re.escape(term) + r"\b", text, flags=re.IGNORECASE))
    return text.lower().count(term.lower())


def scan(text: str, guide: dict) -> list[dict]:
    """Return violations: ``[{term, reason, count}]`` for rules that trip.

    A rule with ``limit=0`` trips on any occurrence; ``limit=N`` only past N.
    """
    if not text:
        return []
    violations: list[dict] = []
    for rule in guide.get("rules", []):
        n = _count(rule["term"], rule["is_word"], text)
        if n > rule["limit"]:
            violations.append({"term": rule["term"], "reason": rule["reason"], "count": n})
    return violations
