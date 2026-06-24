"""
Import Tier A companies from the Google Sheet into the companies table.

Sheet: 1iyOD64_xfalt35JqHGaq-h1TzkZOcVwBoG3cE09bMYc → "Tier A" tab
Columns: Company | Category | What they do | Headcount | Stage / Funding | Remote? | Nearest HQ to Auburn CA

Run once from project root:
    python3 scripts/import_tier_a_from_sheet.py
"""

import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.append(str(Path(__file__).parent.parent))

from app.models import get_db, init_db

SHEET_ID = "1iyOD64_xfalt35JqHGaq-h1TzkZOcVwBoG3cE09bMYc"
RANGE = "Tier A!A1:G60"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_creds():
    from google.oauth2.credentials import Credentials

    token_content = os.environ.get("TOKEN_JSON_CONTENT", "")
    if not token_content:
        token_path = Path(__file__).parent.parent / "token.json"
        if token_path.exists():
            token_content = token_path.read_text()
    if not token_content:
        raise RuntimeError("No Google credentials. Set TOKEN_JSON_CONTENT or provide token.json.")
    info = json.loads(token_content)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return creds


def fetch_tier_a_rows() -> list[dict]:
    creds = _get_creds()
    encoded = quote(RANGE)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{encoded}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {creds.token}"})
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("values", [])
    if not rows:
        return []
    headers = rows[0]
    return [dict(zip(headers, row)) for row in rows[1:] if row]


def import_companies(dry_run: bool = False):
    init_db()
    rows = fetch_tier_a_rows()
    print(f"Fetched {len(rows)} companies from sheet.")

    today = str(date.today())
    inserted = 0
    updated = 0

    with get_db() as conn:
        for row in rows:
            name = (row.get("Company") or "").strip()
            if not name:
                continue

            industry_category = (row.get("Category") or "").strip()
            why_interesting = (row.get("What they do") or "").strip()
            headcount_estimate = (row.get("Headcount") or "").strip()
            funding_stage = (row.get("Stage / Funding") or "").strip()
            remote_friendly = (row.get("Remote?") or "").strip()
            nearest_hq = (row.get("Nearest HQ to Auburn CA") or "").strip()

            existing = conn.execute(
                "SELECT id FROM companies WHERE name = ?", (name,)
            ).fetchone()

            if dry_run:
                action = "UPDATE" if existing else "INSERT"
                print(f"  [{action}] {name} | {industry_category} | {remote_friendly} | {nearest_hq}")
                continue

            if existing:
                conn.execute(
                    """UPDATE companies SET
                       tier_a = 1,
                       industry_category = ?,
                       why_interesting = COALESCE(why_interesting, ?),
                       headcount_estimate = ?,
                       funding_stage = ?,
                       remote_friendly = ?,
                       nearest_hq = ?,
                       updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (
                        industry_category,
                        why_interesting,
                        headcount_estimate,
                        funding_stage,
                        remote_friendly,
                        nearest_hq,
                        existing["id"],
                    ),
                )
                updated += 1
                print(f"  [updated] {name}")
            else:
                conn.execute(
                    """INSERT INTO companies
                       (name, sector, why_interesting, headcount_estimate, funding_stage,
                        industry_category, remote_friendly, nearest_hq,
                        tier_a, date_added, source, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'tier_a_import', 'Watchlist')""",
                    (
                        name,
                        industry_category,
                        why_interesting,
                        headcount_estimate,
                        funding_stage,
                        industry_category,
                        remote_friendly,
                        nearest_hq,
                        today,
                    ),
                )
                inserted += 1
                print(f"  [inserted] {name}")

    if not dry_run:
        print(f"\nDone. {inserted} inserted, {updated} updated.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN — no DB writes.\n")
    import_companies(dry_run=dry_run)
