"""Interview performance dashboard metrics for /settings/interviews.

Unifies interview session data (scores by dimension, cadence) and cross-references
against funnel outcomes (applied jobs → accepted/declined). Every aggregate carries
its sample size and confidence gate; nothing is shown without sufficient data.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.config import CALIBRATION_MIN_SAMPLE, BOARD_CONFIDENCE_SOLID_N
from app.models import get_db
from app.services.metrics_service import _metric, _confidence
from app.pipeline.tracker import STAGES


def interview_dashboard_metrics() -> dict:
    """
    Dashboard metrics for interview performance tracking.

    # DATA CONTRACT (consumed by settings_interviews.html):
    {
      "score_trends": [
        {
          "dimension": str,
          "trend": [{"date": "YYYY-MM-DD", "score": float|None, "n": int}],
          "avg": {"value": float|None, "n": int, "confidence": str}
        }
      ],
      "session_cadence": [{"week": "YYYY-WXX", "count": int}],
      "funnel_cross_ref": {
        "they_declined": {"avg_score": {...}, "count": int},
        "i_declined": {"avg_score": {...}, "count": int},
        "accepted": {"avg_score": {...}, "count": int},
        "other": {"avg_score": {...}, "count": int}
      },
      "total_sessions": int,
      "empty": bool
    }
    """
    DIMENSIONS = [
        "narrative_clarity",
        "evidence_specificity",
        "question_handling",
        "executive_presence",
        "fit_differentiation",
        "curiosity_questions",
    ]

    with get_db() as conn:
        # Get all interview sessions with their scores
        sessions = conn.execute(
            """
            SELECT
              s.id,
              s.job_id,
              s.schedule_date,
              s.transcript_insights_json,
              j.pipeline_stage
            FROM interview_sessions s
            JOIN jobs j ON j.id = s.job_id
            WHERE s.schedule_date IS NOT NULL
            ORDER BY s.schedule_date ASC
            """
        ).fetchall()

        # Parse session scores
        session_data = []
        for session in sessions:
            date_str = session["schedule_date"]
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except (ValueError, TypeError):
                continue

            insights = {}
            if session["transcript_insights_json"]:
                try:
                    insights = json.loads(session["transcript_insights_json"]).get(
                        "self_eval_scores", {}
                    )
                except (json.JSONDecodeError, TypeError):
                    pass

            session_data.append({
                "date": date_str,
                "job_id": session["job_id"],
                "pipeline_stage": session["pipeline_stage"],
                "scores": insights,
            })

        # 1. Score trends by dimension over time
        score_trends = []
        for dim in DIMENSIONS:
            trend = []
            for sesh in session_data:
                score = sesh["scores"].get(dim)
                trend.append({
                    "date": sesh["date"],
                    "score": score,
                    "n": 1 if score is not None else 0,
                })

            # Compute average for this dimension
            scores = [s["score"] for s in trend if s["score"] is not None]
            if scores:
                avg_val = round(sum(scores) / len(scores), 2)
            else:
                avg_val = None
            avg_metric = _metric(avg_val, len(scores))

            score_trends.append({
                "dimension": dim,
                "trend": trend,
                "avg": avg_metric,
            })

        # 2. Session cadence by week
        cadence_by_week = {}
        for sesh in session_data:
            date_obj = datetime.strptime(sesh["date"], "%Y-%m-%d")
            week_key = date_obj.strftime("%Y-W%U")
            cadence_by_week[week_key] = cadence_by_week.get(week_key, 0) + 1

        session_cadence = [
            {"week": week, "count": count}
            for week, count in sorted(cadence_by_week.items())
        ]

        # 3. Funnel cross-reference: avg interview score by pipeline outcome
        # Only include jobs that are NOT sync_stub (placeholder rows)
        funnel_cross_ref = {
            "they_declined": {"avg_score": None, "count": 0},
            "i_declined": {"avg_score": None, "count": 0},
            "accepted": {"avg_score": None, "count": 0},
            "other": {"avg_score": None, "count": 0},
        }

        # Compute average score per session (mean of all dimensions), then group
        # by job so a job with multiple sessions (prep + live + debrief) averages
        # across all of them instead of only the last one processed.
        job_session_avgs: dict[int, list[float]] = {}
        for sesh in session_data:
            if sesh["scores"]:
                avg = sum(sesh["scores"].values()) / len(sesh["scores"])
                job_session_avgs.setdefault(sesh["job_id"], []).append(avg)

        session_avg_scores = {
            job_id: sum(avgs) / len(avgs) for job_id, avgs in job_session_avgs.items()
        }

        # Fetch jobs with terminal stages (excluding sync_stub)
        terminal_stages = tuple(
            code for code, meta in STAGES.items() if meta.get("terminal")
        )
        placeholders = ", ".join("?" * len(terminal_stages))
        jobs_by_stage = conn.execute(
            f"""
            SELECT id, pipeline_stage FROM jobs
            WHERE pipeline_stage IN ({placeholders})
            AND pipeline_stage != 'sync_stub'
            """,
            terminal_stages,
        ).fetchall()

        # Group sessions by funnel outcome
        for job in jobs_by_stage:
            job_id = job["id"]
            stage = job["pipeline_stage"]

            # Map stage to funnel outcome category
            if stage == "they_declined":
                category = "they_declined"
            elif stage == "i_declined":
                category = "i_declined"
            elif stage == "accepted":
                category = "accepted"
            else:
                category = "other"

            if job_id in session_avg_scores:
                funnel_cross_ref[category]["count"] += 1
                if "scores_list" not in funnel_cross_ref[category]:
                    funnel_cross_ref[category]["scores_list"] = []
                funnel_cross_ref[category]["scores_list"].append(
                    session_avg_scores[job_id]
                )

        # Compute averages with confidence gating
        for category in funnel_cross_ref:
            scores_list = funnel_cross_ref[category].pop("scores_list", [])
            if scores_list:
                avg_score = round(sum(scores_list) / len(scores_list), 2)
            else:
                avg_score = None
            funnel_cross_ref[category]["avg_score"] = _metric(
                avg_score, funnel_cross_ref[category]["count"]
            )

        total_sessions = len(session_data)

        return {
            "score_trends": score_trends,
            "session_cadence": session_cadence,
            "funnel_cross_ref": funnel_cross_ref,
            "total_sessions": total_sessions,
            "empty": total_sessions == 0,
        }
