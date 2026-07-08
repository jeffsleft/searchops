"""
Monthly progress snapshot cron: captures funnel metrics and optionally appends to wins.md.

Triggered by Modal cron on the 1st of each month at 09:00 UTC.
Computes: jobs_scored, companies_covered, apps_sent, interviews, offers,
median_days_to_first_interview, calibration_hit_rate_json.

If WINS_AUTOEMIT_ENABLED, auto-appends a delta entry to docs/wins.md.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from app.models import get_db
from app.config import WINS_AUTOEMIT_ENABLED


def snapshot_progress() -> dict:
    """
    Compute and persist monthly progress snapshot.

    Returns dict with snapshot_date and all computed metrics.
    """
    from app.services.calibration_service import get_calibration_summary

    snapshot_date = datetime.utcnow().strftime("%Y-%m-%d")

    with get_db() as conn:
        # Count jobs with final_score (scored)
        jobs_scored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE final_score IS NOT NULL AND auto_rejected = 0"
        ).fetchone()[0]

        # Count distinct companies on scored jobs
        companies_covered = conn.execute(
            "SELECT COUNT(DISTINCT company_id) FROM jobs WHERE final_score IS NOT NULL AND auto_rejected = 0"
        ).fetchone()[0]

        # Count jobs reaching 'applied' outcome
        apps_sent = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome = 'applied'"
        ).fetchone()[0]

        # Count jobs reaching 'phone_screen' outcome
        phone_screens = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome IN ('phone_screen', 'interview', 'offer')"
        ).fetchone()[0]

        # Count jobs reaching 'interview' outcome
        interviews = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome IN ('interview', 'offer')"
        ).fetchone()[0]

        # Count jobs reaching 'offer' outcome
        offers = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome = 'offer'"
        ).fetchone()[0]

        # Median days to first interview (from applied_at to first interview outcome)
        dti_rows = conn.execute(
            """
            SELECT j.applied_at, MIN(ao.recorded_at) AS first_interview
            FROM jobs j
            JOIN application_outcomes ao ON ao.job_id = j.id AND ao.outcome = 'interview'
            WHERE j.applied_at IS NOT NULL
            GROUP BY j.id
            """
        ).fetchall()

    days_list = []
    for row in dti_rows:
        if row["applied_at"] and row["first_interview"]:
            try:
                from datetime import datetime as dt
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        applied = dt.strptime(row["applied_at"][:19], fmt)
                        break
                    except ValueError:
                        continue
                else:
                    continue

                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        first_int = dt.strptime(row["first_interview"][:19], fmt)
                        break
                    except ValueError:
                        continue
                else:
                    continue

                days = (first_int - applied).total_seconds() / 86400.0
                days_list.append(days)
            except Exception:
                pass

    median_days_to_first_interview = None
    if days_list:
        days_list.sort()
        median_days_to_first_interview = round(days_list[len(days_list) // 2], 1)

    # Get calibration hit rates
    cal_summary = get_calibration_summary()
    hit_rate_by_bucket = cal_summary.get("hit_rate_by_bucket", [])
    calibration_hit_rate_json = json.dumps(hit_rate_by_bucket) if hit_rate_by_bucket else None

    # Construct raw_json for auditability
    raw_json = json.dumps({
        "snapshot_date": snapshot_date,
        "jobs_scored": jobs_scored,
        "companies_covered": companies_covered,
        "apps_sent": apps_sent,
        "phone_screens": phone_screens,
        "interviews": interviews,
        "offers": offers,
        "median_days_to_first_interview": median_days_to_first_interview,
        "calibration_hit_rate_json": hit_rate_by_bucket,
    })

    # Write to progress_snapshots (INSERT OR REPLACE on snapshot_date)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO progress_snapshots
            (snapshot_date, jobs_scored, companies_covered, apps_sent, phone_screens, interviews, offers,
             calibration_hit_rate_json, median_days_to_first_interview, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
              jobs_scored=excluded.jobs_scored,
              companies_covered=excluded.companies_covered,
              apps_sent=excluded.apps_sent,
              phone_screens=excluded.phone_screens,
              interviews=excluded.interviews,
              offers=excluded.offers,
              calibration_hit_rate_json=excluded.calibration_hit_rate_json,
              median_days_to_first_interview=excluded.median_days_to_first_interview,
              raw_json=excluded.raw_json
            """,
            (
                snapshot_date,
                jobs_scored,
                companies_covered,
                apps_sent,
                phone_screens,
                interviews,
                offers,
                calibration_hit_rate_json,
                median_days_to_first_interview,
                raw_json,
            ),
        )

    result = {
        "snapshot_date": snapshot_date,
        "jobs_scored": jobs_scored,
        "companies_covered": companies_covered,
        "apps_sent": apps_sent,
        "phone_screens": phone_screens,
        "interviews": interviews,
        "offers": offers,
        "median_days_to_first_interview": median_days_to_first_interview,
    }

    # Auto-emit wins.md entry if enabled
    if WINS_AUTOEMIT_ENABLED:
        try:
            _append_wins_md_entry(snapshot_date, result)
        except Exception as e:
            logging.warning(f"[progress] Failed to auto-emit wins.md entry: {e}")

    return result


def _append_wins_md_entry(snapshot_date: str, current_snapshot: dict) -> None:
    """
    Append a progress snapshot entry to docs/wins.md.

    First snapshot says "baseline established"; subsequent snapshots show deltas
    plus Conversion funnel rates with per-stage n values.
    """
    wins_path = Path(__file__).parent.parent.parent / "docs" / "wins.md"
    if not wins_path.exists():
        logging.warning(f"[progress] wins.md not found at {wins_path}")
        return

    with get_db() as conn:
        # Find prior snapshot (one row before current by snapshot_date)
        prior = conn.execute(
            "SELECT * FROM progress_snapshots WHERE snapshot_date < ? ORDER BY snapshot_date DESC LIMIT 1",
            (snapshot_date,),
        ).fetchone()

    # Build entry content
    if not prior:
        # First snapshot
        baseline_text = "first snapshot — baseline established"
        result_lines = [
            f"- **Jobs scored:** {current_snapshot['jobs_scored']}",
            f"- **Companies covered:** {current_snapshot['companies_covered']}",
            f"- **Applications sent:** {current_snapshot['apps_sent']}",
            f"- **Interviews:** {current_snapshot['interviews']}",
            f"- **Offers:** {current_snapshot['offers']}",
        ]
        if current_snapshot["median_days_to_first_interview"] is not None:
            result_lines.append(f"- **Median days to first interview:** {current_snapshot['median_days_to_first_interview']}")

        # For first snapshot, conversion is just the raw counts
        conversion_text = _format_conversion_line(current_snapshot, prior=None)
    else:
        prior_dict = dict(prior)
        baseline_lines = [
            f"Jobs scored: {prior_dict.get('jobs_scored', 0)}",
            f"Companies: {prior_dict.get('companies_covered', 0)}",
            f"Apps: {prior_dict.get('apps_sent', 0)}",
            f"Interviews: {prior_dict.get('interviews', 0)}",
            f"Offers: {prior_dict.get('offers', 0)}",
        ]
        baseline_text = " → ".join(baseline_lines)

        result_lines = [
            f"- **Jobs scored:** {current_snapshot['jobs_scored']} ({current_snapshot['jobs_scored'] - prior_dict.get('jobs_scored', 0):+d})",
            f"- **Companies covered:** {current_snapshot['companies_covered']} ({current_snapshot['companies_covered'] - prior_dict.get('companies_covered', 0):+d})",
            f"- **Applications sent:** {current_snapshot['apps_sent']} ({current_snapshot['apps_sent'] - prior_dict.get('apps_sent', 0):+d})",
            f"- **Interviews:** {current_snapshot['interviews']} ({current_snapshot['interviews'] - prior_dict.get('interviews', 0):+d})",
            f"- **Offers:** {current_snapshot['offers']} ({current_snapshot['offers'] - prior_dict.get('offers', 0):+d})",
        ]
        if current_snapshot["median_days_to_first_interview"] is not None:
            prior_dti = prior_dict.get("median_days_to_first_interview")
            if prior_dti is not None:
                delta = current_snapshot["median_days_to_first_interview"] - prior_dti
                result_lines.append(
                    f"- **Median days to first interview:** {current_snapshot['median_days_to_first_interview']} "
                    f"({'↓' if delta < 0 else '↑'} {abs(delta)} days)"
                )
            else:
                result_lines.append(f"- **Median days to first interview:** {current_snapshot['median_days_to_first_interview']}")

        conversion_text = _format_conversion_line(current_snapshot, prior_dict)

    # Read existing wins.md
    content = wins_path.read_text("utf-8")

    # Find the insertion point (after the "---" separator, before existing entries)
    lines = content.split("\n")
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.strip() == "---" and i > 0:
            insert_idx = i + 1
            break

    # Build new entry
    entry_lines = [
        "",
        f"## SearchOps progress snapshot — {snapshot_date}",
        "",
        "- **Objective:** demonstrate the tool drives a real job-search funnel",
        f"- **Baseline:** {baseline_text}",
        "- **Result:**",
    ] + result_lines + [
        f"- **Conversion:** {conversion_text}",
        "- **Proof:** progress_snapshots row {}; /settings/calibration".format(snapshot_date),
        "",
    ]

    # Insert entry
    new_lines = lines[:insert_idx] + entry_lines + lines[insert_idx:]
    new_content = "\n".join(new_lines)
    wins_path.write_text(new_content, "utf-8")
    logging.info(f"[progress] Appended wins.md entry for {snapshot_date}")


def _format_conversion_line(current: dict, prior: dict | None) -> str:
    """Format the Conversion line showing funnel rates with n. Rates are computed live
    from application_outcomes (not from the `current` snapshot dict) so this reflects
    the true per-outcome funnel regardless of which snapshot is being narrated."""
    with get_db() as conn:
        applied_count = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome = 'applied'"
        ).fetchone()[0]
        screen_count = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome = 'phone_screen'"
        ).fetchone()[0]
        interview_count = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome IN ('interview', 'offer')"
        ).fetchone()[0]
        offer_count = conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM application_outcomes WHERE outcome = 'offer'"
        ).fetchone()[0]

    def _rate_str(num, den):
        if den < 5:
            return f"n={den} (directional)"
        if den == 0:
            return "0%"
        rate = round((num / den) * 100, 1)
        return f"{rate}% (n={den})"

    applied_to_screen = _rate_str(screen_count, applied_count)
    screen_to_interview = _rate_str(interview_count, screen_count) if screen_count else "n/a"
    interview_to_offer = _rate_str(offer_count, interview_count) if interview_count else "n/a"

    return f"applied→screen {applied_to_screen}, screen→interview {screen_to_interview}, interview→offer {interview_to_offer}"
