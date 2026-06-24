"""
Scoring calibration analysis: predicted interview probability vs. actual interview outcomes.

This module measures whether the engine's predicted interview_probability ("High"|"Medium"|"Low"|"Unknown")
and final_score bands correlate with actual interview progression (reaching recruiter/HM/panel/final/accepted stages).

DEFERRED: Automatic Layer-4 re-weighting based on calibration results is explicitly out of scope.
The engine needs ~20–30 interview outcomes to build a statistically meaningful re-weighting model.
This will be revisited once the sample size is adequate (tracked in the ambition backlog).
"""

from app.models import get_db


INTERVIEW_STAGES = {'recruiter', 'hm_interview', 'panel', 'final_offer', 'accepted'}


def compute_calibration() -> dict:
    """
    Compute calibration statistics: predicted vs. actual interview outcomes.

    Returns:
        {
            "by_probability": [
                {"label": "High", "total": n, "got_interview": m, "rate": x.x},
                ...
            ],
            "by_score": [
                {"label": "[8-10]", "total": n, "got_interview": m, "rate": x.x},
                ...
            ],
            "total": n,
            "total_got_interview": m
        }
    """
    with get_db() as conn:
        # Step 1: Fetch all scored, non-auto-rejected jobs
        jobs = conn.execute("""
            SELECT
                id,
                final_score,
                interview_probability,
                pipeline_stage
            FROM jobs
            WHERE auto_rejected = 0
              AND jd_text IS NOT NULL
              AND pipeline_stage NOT IN ('discovered', 'duplicate')
        """).fetchall()

        # Step 2: Fetch set of job IDs that have ever transitioned to an interview stage
        interview_stage_jobs = set(
            row['job_id'] for row in conn.execute("""
                SELECT DISTINCT job_id
                FROM pipeline_history
                WHERE to_stage IN (?, ?, ?, ?, ?)
            """, tuple(INTERVIEW_STAGES)).fetchall()
        )

    # Step 3: Build calibration data
    calibration_by_probability = {}
    calibration_by_score = {}

    for job in jobs:
        job_id = job['id']
        final_score = job['final_score'] or 0
        interview_probability = job['interview_probability'] or 'Unknown'
        pipeline_stage = job['pipeline_stage'] or ''

        # Normalize interview_probability
        if interview_probability not in ('High', 'Medium', 'Low'):
            interview_probability = 'Unknown'

        # Determine if job got an interview (from history OR current stage)
        got_interview = (
            job_id in interview_stage_jobs or
            pipeline_stage.lower() in INTERVIEW_STAGES
        )

        # Bucket by probability
        if interview_probability not in calibration_by_probability:
            calibration_by_probability[interview_probability] = {'total': 0, 'got_interview': 0}
        calibration_by_probability[interview_probability]['total'] += 1
        if got_interview:
            calibration_by_probability[interview_probability]['got_interview'] += 1

        # Bucket by score band
        if final_score >= 8:
            score_band = '[8-10]'
        elif final_score >= 7:
            score_band = '[7-8)'
        elif final_score >= 6:
            score_band = '[6-7)'
        elif final_score >= 5:
            score_band = '[5-6)'
        else:
            score_band = '[0-5)'

        if score_band not in calibration_by_score:
            calibration_by_score[score_band] = {'total': 0, 'got_interview': 0}
        calibration_by_score[score_band]['total'] += 1
        if got_interview:
            calibration_by_score[score_band]['got_interview'] += 1

    # Step 4: Convert to list format with rates, in fixed order
    probability_order = ['High', 'Medium', 'Low', 'Unknown']
    by_probability = []
    for label in probability_order:
        data = calibration_by_probability.get(label, {'total': 0, 'got_interview': 0})
        rate = (data['got_interview'] / data['total'] * 100) if data['total'] > 0 else 0
        by_probability.append({
            'label': label,
            'total': data['total'],
            'got_interview': data['got_interview'],
            'rate': rate
        })

    score_band_order = ['[8-10]', '[7-8)', '[6-7)', '[5-6)', '[0-5)']
    by_score = []
    for label in score_band_order:
        data = calibration_by_score.get(label, {'total': 0, 'got_interview': 0})
        rate = (data['got_interview'] / data['total'] * 100) if data['total'] > 0 else 0
        by_score.append({
            'label': label,
            'total': data['total'],
            'got_interview': data['got_interview'],
            'rate': rate
        })

    # Step 5: Compute totals
    total = sum(j['total'] for j in by_probability)
    total_got_interview = sum(j['got_interview'] for j in by_probability)

    return {
        'by_probability': by_probability,
        'by_score': by_score,
        'total': total,
        'total_got_interview': total_got_interview
    }
