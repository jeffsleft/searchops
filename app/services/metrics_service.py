"""Read-only metric aggregation for the SearchOps self-reporting layer.

Every function is a pure read over real rows. Anti-fabrication is enforced by
`_metric()`: a value computed on a sample below its confidence gate is returned as
`None` with `confidence="insufficient"`, never as a bare number. Templates render
"Insufficient data — n=X" for those; they never invent a 0.

Sources: usage_events + error_events (app/observability capture), task_log
(background jobs), application_outcomes + jobs + score_history (engine quality),
pipeline_history (integrity of the funnel capture).

These functions feed /settings/health. Funnel/calibration for the board view live
in app/services/progress_service.py, which reuses the existing calibration service.
"""
from __future__ import annotations

from app.config import CALIBRATION_MIN_SAMPLE, BOARD_CONFIDENCE_SOLID_N
from app.models import get_db
from app.pipeline.tracker import STAGES

# Terminal stages carry an application_outcomes expectation; the two initial
# stages are "not yet in the funnel" and are excluded from integrity nags.
TERMINAL_STAGES = tuple(code for code, meta in STAGES.items() if meta.get("terminal"))
INITIAL_STAGES = ("discovered", "identified")

# Feature grouping: route_template prefix → feature name, first match wins.
# Prefix rules (not a per-route dict) so new routes classify without maintenance.
FEATURE_RULES = [
    ("/settings", "Settings"),
    ("/guide", "Settings"),
    ("/admin", "Settings"),
    ("/prep", "Interview Prep"),
    ("/recruiters", "Recruiters"),
    ("/followups", "Follow-ups"),
    ("/pipeline", "Pipeline"),
    ("/discovered", "Discovery"),
    ("/targets", "Discovery"),
    ("/companies", "Discovery"),
    ("/api/discovery", "Discovery"),
    ("/vetting", "Dashboard"),
    ("/rejected", "Dashboard"),
    ("/job", "Jobs & Scoring"),
    ("/", "Dashboard"),
]
# Route templates intentionally retired — excluded from the cold-feature nag.
DEPRECATED_ROUTES: set[str] = set()


def _confidence(n: int) -> str:
    if n >= BOARD_CONFIDENCE_SOLID_N:
        return "solid"
    if n >= CALIBRATION_MIN_SAMPLE:
        return "directional"
    return "insufficient"


def _metric(value, n: int) -> dict:
    """Wrap a computed value with its sample size and confidence. Below the minimum
    gate the value is suppressed (None) so no scalar is shown on thin data."""
    conf = _confidence(n)
    return {"value": None if conf == "insufficient" else value, "n": n, "confidence": conf}


def _feature_for(route_template: str) -> str:
    for prefix, name in FEATURE_RULES:
        if prefix == "/":
            if route_template == "/":
                return name
        elif route_template.startswith(prefix):
            return name
    return "Other"


def _percentile(sorted_vals: list[int], pct: float) -> int | None:
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def usage_summary(window_days: int = 30) -> dict:
    """Feature-usage rollup from usage_events.

    # DATA CONTRACT (consumed by settings_health.html):
    {
      "window_days": int,
      "features": [{"name": str, "state": "active"|"cold"|"never",
                    "hits": int, "last_used": str|None, "error_rate": float}],
      "hot_paths": [{"route": str, "hits": int}],       # top 10 by hits in window
      "cold_features": [str],                            # feature names, 0 hits in window
      "total_events": int
    }
    """
    cutoff = f"-{int(window_days)} days"
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT route_template,
                   COUNT(*) AS hits,
                   MAX(ts) AS last_used,
                   SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors
            FROM usage_events
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            GROUP BY route_template
            """,
            (cutoff,),
        ).fetchall()
        # "never" needs the full route surface, not just what's been hit — but we only
        # know routes that have events; treat features with zero rows as cold/never below.
        total = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S','now',?)",
            (cutoff,),
        ).fetchone()[0]

    # Aggregate per feature.
    feat: dict[str, dict] = {}
    hot: list[dict] = []
    for r in rows:
        rt = r["route_template"]
        if rt in ("<unmatched>", "<unknown>"):
            continue
        hot.append({"route": rt, "hits": r["hits"]})
        f = feat.setdefault(_feature_for(rt), {"hits": 0, "errors": 0, "last_used": None})
        f["hits"] += r["hits"]
        f["errors"] += r["errors"] or 0
        if r["last_used"] and (f["last_used"] is None or r["last_used"] > f["last_used"]):
            f["last_used"] = r["last_used"]

    all_features = sorted({name for _, name in FEATURE_RULES} | {"Other"})
    features = []
    for name in all_features:
        f = feat.get(name)
        if not f or f["hits"] == 0:
            features.append({"name": name, "state": "never", "hits": 0, "last_used": None, "error_rate": 0.0})
        else:
            err_rate = round((f["errors"] / f["hits"]) * 100, 1) if f["hits"] else 0.0
            features.append({
                "name": name, "state": "active", "hits": f["hits"],
                "last_used": f["last_used"], "error_rate": err_rate,
            })
    cold_features = [f["name"] for f in features if f["hits"] == 0 and f["name"] not in DEPRECATED_ROUTES]
    hot.sort(key=lambda x: x["hits"], reverse=True)
    return {
        "window_days": window_days,
        "features": features,
        "hot_paths": hot[:10],
        "cold_features": cold_features,
        "total_events": total,
    }


def system_health(window_days: int = 7) -> dict:
    """Request volume, error rate, latency percentiles, and background-job freshness.

    # DATA CONTRACT (consumed by settings_health.html):
    {
      "window_days": int,
      "req_count": int, "error_count": int, "error_rate": float,
      "p50_ms": int|None, "p95_ms": int|None,
      "crons": [{"task_type": str, "last_ok": str|None}],
      "recent_errors": [{"ts": str, "route_template": str, "exc_type": str, "message": str}]
    }
    """
    cutoff = f"-{int(window_days)} days"
    with get_db() as conn:
        req_count = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S','now',?)",
            (cutoff,),
        ).fetchone()[0]
        error_count = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE status >= 500 AND ts >= strftime('%Y-%m-%dT%H:%M:%S','now',?)",
            (cutoff,),
        ).fetchone()[0]
        durations = [
            r[0] for r in conn.execute(
                "SELECT duration_ms FROM usage_events "
                "WHERE duration_ms IS NOT NULL AND ts >= strftime('%Y-%m-%dT%H:%M:%S','now',?) "
                "ORDER BY duration_ms",
                (cutoff,),
            ).fetchall()
        ]
        crons = conn.execute(
            "SELECT task_type, MAX(logged_at) AS last_ok FROM task_log "
            "WHERE status = 'completed' GROUP BY task_type ORDER BY task_type"
        ).fetchall()
        recent = conn.execute(
            "SELECT ts, route_template, exc_type, message FROM error_events ORDER BY id DESC LIMIT 10"
        ).fetchall()

    return {
        "window_days": window_days,
        "req_count": req_count,
        "error_count": error_count,
        "error_rate": round((error_count / req_count) * 100, 2) if req_count else 0.0,
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "crons": [{"task_type": c["task_type"], "last_ok": c["last_ok"]} for c in crons],
        "recent_errors": [dict(r) for r in recent],
    }


def engine_quality() -> dict:
    """Does the scoring engine actually predict outcomes?

    # DATA CONTRACT (consumed by settings_health.html):
    {
      "score_cohort": [{"band": str, "n": int, "interview_rate": float|None, "confidence": str}],
      "score_stability": {"value": float|None, "n": int, "confidence": str},   # mean abs re-score delta
      "corpus_coverage": {"value": float|None, "n": int, "confidence": str}    # % scored jobs w/ Layer-2 match
    }
    An "interview-or-better" outcome = the job reached 'interview' or 'offer' in
    application_outcomes. Bands are on jobs.final_score.
    """
    bands = [("≥8 (dream/solid)", 8.0, 10.01), ("5–8 (worth a look)", 5.0, 8.0), ("<5 (low)", 0.0, 5.0)]
    cohort = []
    with get_db() as conn:
        for label, lo, hi in bands:
            n = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE final_score >= ? AND final_score < ? AND auto_rejected = 0",
                (lo, hi),
            ).fetchone()[0]
            got = conn.execute(
                """
                SELECT COUNT(DISTINCT j.id) FROM jobs j
                JOIN application_outcomes ao ON ao.job_id = j.id
                WHERE j.final_score >= ? AND j.final_score < ? AND j.auto_rejected = 0
                  AND ao.outcome IN ('interview', 'offer')
                """,
                (lo, hi),
            ).fetchone()[0]
            m = _metric(round((got / n) * 100, 1) if n else 0.0, n)
            cohort.append({"band": label, "n": n, "interview_rate": m["value"], "confidence": m["confidence"]})

        # Score stability: mean absolute delta across consecutive re-scores per job.
        deltas = []
        hist = conn.execute(
            "SELECT job_id, final_score FROM score_history WHERE final_score IS NOT NULL ORDER BY job_id, scored_at"
        ).fetchall()
        by_job: dict[int, list[float]] = {}
        for h in hist:
            by_job.setdefault(h["job_id"], []).append(h["final_score"])
        for scores in by_job.values():
            for a, b in zip(scores, scores[1:]):
                deltas.append(abs(b - a))
        stab = _metric(round(sum(deltas) / len(deltas), 2) if deltas else 0.0, len(deltas))

        # Corpus coverage: of scored (non-rejected) jobs, share with a non-zero Layer-2 match.
        scored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE final_score IS NOT NULL AND auto_rejected = 0"
        ).fetchone()[0]
        covered = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE final_score IS NOT NULL AND auto_rejected = 0 "
            "AND match_score IS NOT NULL AND match_score != 0"
        ).fetchone()[0]
        cov = _metric(round((covered / scored) * 100, 1) if scored else 0.0, scored)

    return {
        "score_cohort": cohort,
        "score_stability": stab,
        "corpus_coverage": cov,
    }


def integrity_checks() -> dict:
    """Cheap consistency checks that protect the metrics above from silent rot.

    # DATA CONTRACT (consumed by settings_health.html):
    { "checks": [{"check": str, "count": int, "href": str}] }
    """
    placeholders = ",".join("?" for _ in TERMINAL_STAGES) or "''"
    advanced = ",".join("?" for _ in INITIAL_STAGES)
    with get_db() as conn:
        # 1) Terminal-stage jobs with no recorded outcome (capture gap).
        missing_outcome = conn.execute(
            f"""
            SELECT COUNT(*) FROM jobs j
            WHERE j.pipeline_stage IN ({placeholders})
              AND NOT EXISTS (SELECT 1 FROM application_outcomes ao WHERE ao.job_id = j.id)
            """,
            TERMINAL_STAGES,
        ).fetchone()[0]

        # 2) Jobs advanced past the initial stages with ZERO pipeline_history rows
        #    (a stage change happened without a history write — validates the single writer).
        history_gap = conn.execute(
            f"""
            SELECT COUNT(*) FROM jobs j
            WHERE j.pipeline_stage NOT IN ({advanced})
              AND NOT EXISTS (SELECT 1 FROM pipeline_history h WHERE h.job_id = j.id)
            """,
            INITIAL_STAGES,
        ).fetchone()[0]

        # 3) Stale: non-terminal, non-initial jobs untouched for 21+ days.
        stale = conn.execute(
            f"""
            SELECT COUNT(*) FROM jobs j
            WHERE j.pipeline_stage NOT IN ({advanced})
              AND j.pipeline_stage NOT IN ({placeholders})
              AND j.updated_at < strftime('%Y-%m-%d %H:%M:%S', 'now', '-21 days')
            """,
            (*INITIAL_STAGES, *TERMINAL_STAGES),
        ).fetchone()[0]

        # 4) In-pipeline jobs still unvetted for ethics (CLAUDE.md: manual confirmation required).
        unvetted = conn.execute(
            f"""
            SELECT COUNT(*) FROM jobs j
            WHERE j.pipeline_stage NOT IN ({advanced})
              AND j.ethics_vetted = 0
            """,
            INITIAL_STAGES,
        ).fetchone()[0]

    return {
        "checks": [
            {"check": "Terminal jobs missing an outcome", "count": missing_outcome, "href": "/pipeline"},
            {"check": "Advanced jobs with no stage history", "count": history_gap, "href": "/pipeline"},
            {"check": "Stale jobs (21+ days untouched)", "count": stale, "href": "/pipeline"},
            {"check": "In-pipeline jobs not ethics-vetted", "count": unvetted, "href": "/vetting"},
        ]
    }
