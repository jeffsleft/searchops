"""
Pattern analytics: which signals correlate with pipeline progression.

Extracted verbatim from the old inline body of `admin_patterns_get` (routes.py)
as part of the service-layer refactor. Behaviour is intentionally unchanged —
"progressed" is still derived from the job's CURRENT pipeline_stage (which
includes the decline stages). For an interview-accurate predicted-vs-actual
view, see `app.pipeline.calibration` (that is the corrected metric; this one is
kept as-is for continuity of the existing breakdowns).
"""
from app.models import get_db

# Stages treated as "progressed" by the legacy breakdowns. NOTE: includes the
# decline stages, so this over-counts true interviews — see module docstring.
PROGRESSED_STAGES = {
    'recruiter', 'hm_interview', 'panel', 'final_offer',
    'accepted', 'i_declined', 'they_declined',
}


def _score_bucket(score: float) -> str:
    if score < 5:
        return '[0-5)'
    if score < 6:
        return '[5-6)'
    if score < 7:
        return '[6-7)'
    if score < 8:
        return '[7-8)'
    return '[8-10]'


def compute_patterns() -> dict:
    """Compute the progression breakdowns shown on /admin/patterns.

    Returns the template context: total_apps, total_progressed, response_rate
    (str, 1dp), and buckets/sectors/greenfield/archetypes as (key, {total,
    progressed}) lists sorted by total desc.
    """
    with get_db() as conn:
        jobs = conn.execute("""
            SELECT pipeline_stage, final_score, sector, greenfield, role_archetype,
                   flags_json, adjustment_weights_score, match_score
            FROM jobs
            WHERE auto_rejected = 0
              AND pipeline_stage NOT IN ('discovered')
              AND jd_text IS NOT NULL
        """).fetchall()

    jobs_list = [dict(r) for r in jobs]

    buckets = {
        '[0-5)': {'total': 0, 'progressed': 0},
        '[5-6)': {'total': 0, 'progressed': 0},
        '[6-7)': {'total': 0, 'progressed': 0},
        '[7-8)': {'total': 0, 'progressed': 0},
        '[8-10]': {'total': 0, 'progressed': 0},
    }
    sectors: dict = {}
    greenfield_values: dict = {}
    archetypes: dict = {}

    for job in jobs_list:
        is_progressed = job.get('pipeline_stage', '').lower() in PROGRESSED_STAGES

        bucket_key = _score_bucket(job.get('final_score') or 0)
        buckets[bucket_key]['total'] += 1
        if is_progressed:
            buckets[bucket_key]['progressed'] += 1

        sector = job.get('sector', 'Unknown')
        sectors.setdefault(sector, {'total': 0, 'progressed': 0})
        sectors[sector]['total'] += 1
        if is_progressed:
            sectors[sector]['progressed'] += 1

        gf = job.get('greenfield', 'Unknown')
        greenfield_values.setdefault(gf, {'total': 0, 'progressed': 0})
        greenfield_values[gf]['total'] += 1
        if is_progressed:
            greenfield_values[gf]['progressed'] += 1

        arch = job.get('role_archetype', 'Other')
        archetypes.setdefault(arch, {'total': 0, 'progressed': 0})
        archetypes[arch]['total'] += 1
        if is_progressed:
            archetypes[arch]['progressed'] += 1

    total_apps = len(jobs_list)
    total_progressed = sum(
        1 for j in jobs_list if j.get('pipeline_stage', '').lower() in PROGRESSED_STAGES
    )
    response_rate = (total_progressed / total_apps * 100) if total_apps > 0 else 0

    by_total = lambda kv: kv[1]['total']  # noqa: E731
    return {
        'total_apps': total_apps,
        'total_progressed': total_progressed,
        'response_rate': f"{response_rate:.1f}",
        'buckets': sorted(buckets.items(), key=by_total, reverse=True),
        'sectors': sorted(sectors.items(), key=by_total, reverse=True),
        'greenfield': sorted(greenfield_values.items(), key=by_total, reverse=True),
        'archetypes': sorted(archetypes.items(), key=by_total, reverse=True),
    }
