import os
import requests
from app.scoring.engine import classify_score

APP_URL = "https://jeffsleft--recruiting-engine-web.modal.run"


def _webhook_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def _post(payload: dict) -> bool:
    url = _webhook_url()
    if not url:
        return False
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def _slack_escape(text: str) -> str:
    """Escape Slack mrkdwn special characters in user-controlled strings."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_high_score_alert(job_id: int, score_record: dict) -> bool:
    tier_info = classify_score(score_record.get("final_score", 0))
    score = score_record.get("final_score", 0)
    company = _slack_escape(score_record.get("company", "Unknown"))
    role = _slack_escape(score_record.get("job_title", "Unknown"))
    pros = _slack_escape((score_record.get("pros") or "")[:200])
    cons = _slack_escape((score_record.get("cons") or "")[:200])
    link = f"{APP_URL}/job/{job_id}"

    text = (
        f":dart: *High-Value Opportunity — {company}*\n\n"
        f"*Role:* {role}\n"
        f"*Score:* {score}/10 ({tier_info['tier']})\n"
        f"*Greenfield:* {score_record.get('greenfield', 'Unknown')}\n\n"
        f"*Pros:* {pros}\n"
        f"*Cons:* {cons}\n\n"
        f"<{link}|Open in Recruiting Engine>"
    )
    return _post({"text": text})


def send_weekly_digest() -> bool:
    from app.models import get_db
    from app.pipeline.tracker import get_pipeline_summary, get_stale_pipeline
    from app.recruiters.crm import get_stale_recruiters

    summary = get_pipeline_summary()
    stale = get_stale_pipeline()
    stale_recruiters = get_stale_recruiters()

    with get_db() as conn:
        new_this_week = conn.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE date_found >= date('now', '-7 days')"
        ).fetchone()["cnt"]
        avg_row = conn.execute(
            "SELECT AVG(final_score) as avg FROM jobs WHERE final_score IS NOT NULL AND date_found >= date('now', '-7 days')"
        ).fetchone()
        avg_score = avg_row["avg"] if avg_row else None
        top = conn.execute(
            "SELECT company, job_title, final_score FROM jobs WHERE final_score IS NOT NULL ORDER BY final_score DESC LIMIT 1"
        ).fetchone()

    stage_lines = "\n".join(
        f"• {stage}: {count}" for stage, count in summary.items() if count > 0
    ) or "• Pipeline is empty"

    stale_lines = (
        "\n".join(f"• {_slack_escape(s['company'])} ({s['pipeline_stage']})" for s in stale[:5])
        or "None"
    )
    recruiter_lines = (
        "\n".join(f"• {_slack_escape(r['name'])} — {int(r['days_since'])}d since contact" for r in stale_recruiters[:3])
        or "None"
    )

    this_week = f"• {new_this_week} new jobs scored\n"
    if avg_score is not None:
        this_week += f"• Avg score this week: {round(avg_score, 1)}\n"
    if top:
        this_week += f"• Top match: {_slack_escape(top['company'])} — {_slack_escape(top['job_title'])} ({top['final_score']})\n"

    text = (
        f":bar_chart: *Recruiting Engine — Weekly Digest*\n\n"
        f"*Pipeline:*\n{stage_lines}\n\n"
        f"*This Week:*\n{this_week}\n"
        f"*Stale (14+ days without activity):*\n{stale_lines}\n\n"
        f"*Recruiter Follow-ups Due:*\n{recruiter_lines}\n\n"
        f"<{APP_URL}|Open Dashboard>"
    )
    return _post({"text": text})


# Preliminary-score bar for the daily discovery digest. Scores are the
# lightweight discovery estimate, not the full 4-layer final_score.
DISCOVERY_HIGH_SCORE = 8.0


def send_discovery_notification(new_jobs: list[dict]) -> bool:
    """Daily discovery digest — leads with how many new roles cleared the
    high-score bar so the heads-up is actionable, not just a wall of links.

    Each job may carry a preliminary 'score'; high-scorers are listed first
    with their score. Falls back gracefully if 'score' is absent.
    """
    if not new_jobs:
        return True

    def _score(j: dict) -> float:
        s = j.get('score')
        return s if isinstance(s, (int, float)) else 0.0

    high = sorted(
        (j for j in new_jobs if _score(j) >= DISCOVERY_HIGH_SCORE),
        key=_score, reverse=True,
    )
    total = len(new_jobs)
    bar = f"{DISCOVERY_HIGH_SCORE:g}"

    if high:
        headline = f":dart: *Discovery — {len(high)} new role(s) ≥ {bar}* (of {total} found today)"
        body = "\n".join(
            f"• *{_slack_escape(j['company'])}* — {_slack_escape(j['title'])}  _(prelim {_score(j):.1f})_"
            for j in high[:10]
        )
        rest = total - len(high)
        footer = f"\n\n_+{rest} more below {bar} — see Discovered._" if rest > 0 else ""
    else:
        headline = f":mag: *Discovery — {total} new role(s) today* (none ≥ {bar} yet)"
        body = "\n".join(
            f"• {_slack_escape(j['company'])} — {_slack_escape(j['title'])}"
            + (f"  _(prelim {_score(j):.1f})_" if j.get('score') is not None else "")
            for j in new_jobs[:10]
        )
        footer = ""

    text = f"{headline}\n\n{body}{footer}\n\n<{APP_URL}/discovered|View in Recruiting Engine>"
    return _post({"text": text})


def test_webhook() -> bool:
    return _post({"text": ":white_check_mark: Recruiting Engine — webhook test successful."})
