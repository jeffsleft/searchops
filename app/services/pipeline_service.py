"""
Pipeline view data aggregation service extracted from routes.py.

Owns pipeline queries, stage grouping, stale calculations, and job aggregations.
Returns a dict ready for template rendering.
"""
from app.models import get_db
from app.pipeline.tracker import get_stale_pipeline


TERMINAL_STAGES = {'accepted', 'i_declined', 'they_declined', 'job_listing_closed', 'duplicate'}


def build_pipeline_view_data(archetype: str, _enrich_job_fn) -> dict:
    """
    Build pipeline view data: jobs grouped by stage, stale items, counts.

    Args:
        archetype: Selected role archetype filter (empty string = no filter)
        _enrich_job_fn: Callable to enrich a job dict (from routes._enrich_job)

    Returns:
        Dict with keys: jobs_by_stage, stale_items, total, active, max_stage_count
    """
    with get_db() as conn:
        query = "SELECT * FROM jobs WHERE auto_rejected = 0"
        params = []
        if archetype:
            query += " AND role_archetype = ?"
            params.append(archetype)
        query += " ORDER BY final_score DESC"
        rows = conn.execute(query, params).fetchall()

    jobs_by_stage: dict = {}
    for r in rows:
        j = _enrich_job_fn(dict(r))
        stage = j.get("pipeline_stage") or "identified"
        jobs_by_stage.setdefault(stage, []).append(j)

    stale_items = get_stale_pipeline()
    total = len(rows)
    active = sum(
        len(v) for k, v in jobs_by_stage.items()
        if k not in TERMINAL_STAGES
    )
    max_stage_count = max((len(v) for v in jobs_by_stage.values()), default=1)

    return {
        'jobs_by_stage': jobs_by_stage,
        'stale_items': stale_items,
        'total': total,
        'active': active,
        'max_stage_count': max_stage_count,
    }
