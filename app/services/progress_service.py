"""Board-facing progress metrics for /settings/progress.

Lean, honest implementation: reuses the existing calibration service and reads
application_outcomes + jobs directly. When docs/progress-instrumentation-spec-v2.md
lands (progress_snapshots table + monthly cron), funnel_trend should switch to
reading persisted snapshots; until then it derives a month series from live
outcomes so the view renders truthfully today. Every rate carries its n and a
confidence tag; nothing is shown on a sample below the gate.
"""
from __future__ import annotations

from app.config import CALIBRATION_MIN_SAMPLE
from app.models import get_db
from app.pipeline.calibration import compute_calibration
from app.services.metrics_service import _metric, engine_quality


def _stage_count(conn, outcome: str) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome = ?",
        (outcome,),
    ).fetchone()[0]


def board_metrics() -> dict:
    """
    Board-facing metrics: reads from progress_snapshots table (v2).

    # DATA CONTRACT (consumed by settings_progress.html):
    {
      "funnel_trend": [{"month": "YYYY-MM", "applied": int, "screen": int,
                        "interview": int, "offer": int}],
      "conversion": [{"stage": str, "rate": float|None, "n": int, "confidence": str}],
      "velocity": {"days_to_first_interview": {"value": float|None, "n": int, "confidence": str}},
      "targeting": {"interviews_per_10_apps": {"value": float|None, "n": int, "confidence": str},
                    "cohort": [{"band": str, "rate": float|None, "n": int}]},
      "calibration": {"insufficient_data": bool, "buckets": [{"label": str, "rate": float|None, "n": int}]}
    }
    """
    with get_db() as conn:
        # Live counts for conversion calculations
        applied = _stage_count(conn, "applied")
        screen = _stage_count(conn, "phone_screen")
        interview = _stage_count(conn, "interview")
        offer = _stage_count(conn, "offer")

        # Funnel trend from progress_snapshots table
        snapshots = conn.execute(
            "SELECT snapshot_date, apps_sent, phone_screens, interviews, offers FROM progress_snapshots ORDER BY snapshot_date"
        ).fetchall()

        # Days to first interview: first 'interview' outcome vs. jobs.applied_at.
        dti_rows = conn.execute(
            """
            SELECT j.applied_at AS applied_at, MIN(ao.recorded_at) AS first_interview
            FROM jobs j
            JOIN application_outcomes ao ON ao.job_id = j.id AND ao.outcome = 'interview'
            WHERE j.applied_at IS NOT NULL
            GROUP BY j.id
            """
        ).fetchall()

    # Build funnel_trend from snapshots
    funnel_trend = []
    for snap in snapshots:
        month = snap["snapshot_date"][:7]  # Extract YYYY-MM from YYYY-MM-DD
        funnel_trend.append({
            "month": month,
            "applied": snap["apps_sent"],
            "screen": snap["phone_screens"],
            "interview": snap["interviews"],
            "offer": snap["offers"],
        })

    def _rate(num, den):
        m = _metric(round((num / den) * 100, 1) if den else 0.0, den)
        return m
    conversion = [
        {"stage": "Applied → Screen", **_pick(_rate(screen, applied))},
        {"stage": "Screen → Interview", **_pick(_rate(interview, screen))},
        {"stage": "Interview → Offer", **_pick(_rate(offer, interview))},
    ]

    # Median days-to-first-interview.
    days = []
    for r in dti_rows:
        d = _days_between(r["applied_at"], r["first_interview"])
        if d is not None:
            days.append(d)
    days.sort()
    median = days[len(days) // 2] if days else 0.0
    dti = _metric(round(median, 1), len(days))

    interviews_per_10 = _metric(round((interview / applied) * 10, 1) if applied else 0.0, applied)

    eq = engine_quality()
    cohort = [{"band": c["band"], "rate": c["interview_rate"], "n": c["n"]} for c in eq["score_cohort"]]

    cal = compute_calibration()
    buckets = [
        {"label": b["label"], "rate": (b["rate"] if b["total"] >= CALIBRATION_MIN_SAMPLE else None), "n": b["total"]}
        for b in cal.get("by_probability", [])
    ]

    return {
        "funnel_trend": funnel_trend,
        "conversion": conversion,
        "velocity": {"days_to_first_interview": dti},
        "targeting": {"interviews_per_10_apps": interviews_per_10, "cohort": cohort},
        "calibration": {"insufficient_data": cal.get("total", 0) < CALIBRATION_MIN_SAMPLE, "buckets": buckets},
    }


def _pick(metric: dict) -> dict:
    """Flatten a _metric() dict into the conversion row shape."""
    return {"rate": metric["value"], "n": metric["n"], "confidence": metric["confidence"]}


def _days_between(start: str | None, end: str | None) -> float | None:
    from datetime import datetime
    if not start or not end:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            s = datetime.strptime(start[:19], fmt)
            break
        except ValueError:
            continue
    else:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            e = datetime.strptime(end[:19], fmt)
            break
        except ValueError:
            continue
    else:
        return None
    return (e - s).total_seconds() / 86400.0
