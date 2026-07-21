"""
W2 — Application Kit assembly.

One gate-cleared job → a ready-to-send package: Layer 2 evidence, mismatches to address,
tailored resume bullets, cover-letter hooks, a voice-passed cover letter, and the
recommended angle. Every component already existed and rendered separately on the job
detail page; this service is the assembly + the gate.

Gate (both required, per docs/plan-dual-track-2026-07-14.md → Wave 2):
  * final_score >= the profile's top-band threshold (default 7.0 — the "Solid Bet" floor)
  * ethics_vetted manually confirmed — the ethics gate stays manual by charter; nothing
    here ever sets it

A blocked kit still renders, with the failing checks named. It never 404s: the ethics
toggle is the most common blocker and needs to be reachable from the kit itself.

Corpus availability is a *quality signal*, not a gate. A kit built off the fictional
example corpus is labelled as such everywhere it surfaces — see `corpus.is_example`.
"""
from __future__ import annotations

import json
import logging

from app.config import load_profile
from app.models import get_db
from app.scoring.corpus import corpus_status

DEFAULT_GATE_THRESHOLD = 7.0


def _gate_threshold() -> float:
    """Kit gate threshold, from the profile's scoring config."""
    try:
        profile = load_profile()
        scoring = profile.get("scoring") or {}
        return float(scoring.get("top_band_gate_threshold", DEFAULT_GATE_THRESHOLD))
    except Exception as e:
        logging.warning("kit: falling back to default gate threshold: %s", e)
        return DEFAULT_GATE_THRESHOLD


def evaluate_gate(job: dict) -> dict:
    """
    Decide whether a job is kit-ready.

    Returns {"cleared": bool, "threshold": float, "checks": [{key,label,ok,detail}]}.
    Checks are ordered for display; every failing one is shown, not just the first.
    """
    threshold = _gate_threshold()
    score = job.get("final_score") or 0.0
    auto_rejected = bool(job.get("auto_rejected"))
    ethics_ok = bool(job.get("ethics_vetted"))

    checks = [
        {
            "key": "not_rejected",
            "label": "Not auto-rejected",
            "ok": not auto_rejected,
            "detail": job.get("reject_reason") or "" if auto_rejected else "",
        },
        {
            "key": "score",
            "label": f"Score gate {threshold:g}",
            "ok": (not auto_rejected) and float(score) >= threshold,
            "detail": f"{float(score):.1f}",
        },
        {
            "key": "ethics",
            "label": "Ethics vetted",
            "ok": ethics_ok,
            "detail": "" if ethics_ok else "Confirm manually — never set automatically",
        },
    ]
    return {
        "cleared": all(c["ok"] for c in checks),
        "threshold": threshold,
        "checks": checks,
    }


def _json_field(job: dict, col: str) -> list:
    """Read a match_* JSON column off a raw row, tolerating nulls and bad JSON."""
    try:
        val = json.loads(job.get(col) or "[]")
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def build_kit_data(job_id: int, enrich_fn, cover_letter_loader) -> dict | None:
    """
    Assemble the Application Kit for a job. Returns None if the job doesn't exist.

    Args:
        job_id: jobs.id
        enrich_fn: routes._enrich_job — hydrates tier + match_* JSON onto the row
        cover_letter_loader: routes._load_cover_letter — returns the stored letter,
            generating and persisting it on first call. Only invoked when the gate
            clears, so a blocked kit never burns an LLM call.
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None

    raw = dict(row)
    job = enrich_fn(raw)
    gate = evaluate_gate(raw)
    corpus = corpus_status()

    cover_letter = None
    if gate["cleared"]:
        try:
            cover_letter = cover_letter_loader(raw)
        except Exception as e:
            # A failed letter must not take the whole kit down — the evidence, bullets,
            # and mismatches are still worth having in front of Jeff.
            logging.error("kit: cover letter failed for job %s: %s", job_id, e)
            cover_letter = {"error": f"{type(e).__name__}: {e}", "body": []}

    voice = (cover_letter or {}).get("voice") or {}

    return {
        "job": job,
        "gate": gate,
        "corpus": corpus,
        "angle": job.get("recommended_angle") or "",
        "evidence": _json_field(raw, "match_evidence_json"),
        "mismatches": _json_field(raw, "match_mismatches_json"),
        "bullets": _json_field(raw, "match_bullets_json"),
        "hooks": _json_field(raw, "match_hooks_json"),
        "cover_letter": cover_letter,
        "voice": voice,
        "ready": bool(gate["cleared"] and cover_letter and cover_letter.get("body")),
    }
