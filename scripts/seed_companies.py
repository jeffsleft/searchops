"""
One-time seed script: import 247 "Y" companies from Venture Funded Companies List.xlsx
into the SQLite companies table.

Run from project root:
    python3 scripts/seed_companies.py

Safe to re-run — skips companies already present by name (case-insensitive).
"""
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
XLSX = ROOT / "Venture Funded Companies List.xlsx"
DB_PATH = ROOT / "recruiting.db"
SHEET = "WORKING Crunchbase Export updat"


def get_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    """Create companies table if it doesn't exist (minimal version for seeding)."""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            website TEXT,
            sector TEXT,
            headcount_estimate TEXT,
            funding_stage TEXT,
            why_interesting TEXT,
            fit_score REAL,
            need_assessment TEXT DEFAULT 'Unknown',
            source TEXT DEFAULT 'manual',
            date_added DATE NOT NULL,
            status TEXT DEFAULT 'Watchlist',
            research_json TEXT,
            research_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    con.commit()


def load_y_companies(xlsx: Path) -> pd.DataFrame:
    df = pd.read_excel(xlsx, sheet_name=SHEET)

    # Column 1 (index 1, header ' ') is CS Leader Name — rename for clarity
    cols = list(df.columns)
    cols[1] = "CS Leader Name"
    df.columns = cols

    # Keep only Y/y rows
    y = df[df["Interest Y/N/?"].isin(["Y", "y"])].copy()
    y = y.reset_index(drop=True)
    print(f"Loaded {len(y)} companies marked Y")
    return y


def build_research_json(row: pd.Series) -> str:
    """Pack Crunchbase data we have into research_json for later display."""
    data = {}

    if pd.notna(row.get("Organization Description")):
        data["description"] = str(row["Organization Description"])
    if pd.notna(row.get("Organization Location")):
        data["location"] = str(row["Organization Location"])
    if pd.notna(row.get("Total Funding Amount (in USD)")):
        data["total_funding_usd"] = float(row["Total Funding Amount (in USD)"])
    if pd.notna(row.get("Money Raised (in USD)")):
        data["last_round_usd"] = float(row["Money Raised (in USD)"])
    if pd.notna(row.get("Lead Investors")):
        data["lead_investors"] = str(row["Lead Investors"])
    if pd.notna(row.get("Investor Names")):
        data["investors"] = str(row["Investor Names"])
    if pd.notna(row.get("Announced Date")):
        data["last_round_date"] = str(row["Announced Date"])
    if pd.notna(row.get("Organization Name URL")):
        data["crunchbase_url"] = str(row["Organization Name URL"])

    # CS leader info from prior research (if any)
    cs_name = row.get("CS Leader Name")
    cs_linkedin = row.get("CS LinkedIn")
    cs_roles = row.get("CS Roles")
    role_listings = row.get("Role Listings")

    if pd.notna(cs_name) and str(cs_name).strip() not in ("N", ""):
        data["cs_leader_name"] = str(cs_name)
    if pd.notna(cs_linkedin):
        data["cs_leader_linkedin"] = str(cs_linkedin)
    if pd.notna(cs_roles):
        data["cs_open_roles"] = str(cs_roles)
    if pd.notna(role_listings):
        data["cs_role_listings"] = str(role_listings)

    return json.dumps(data)


def seed(dry_run: bool = False) -> None:
    y = load_y_companies(XLSX)

    con = get_db(DB_PATH)
    init_db(con)

    # Build set of existing names for dedup (case-insensitive)
    existing = {
        row[0].lower()
        for row in con.execute("SELECT name FROM companies").fetchall()
    }
    print(f"Existing companies in DB: {len(existing)}")

    inserted = 0
    skipped = 0
    today = date.today().isoformat()

    for _, row in y.iterrows():
        name = row.get("Organization Name")
        if pd.isna(name) or not str(name).strip():
            skipped += 1
            continue

        name = str(name).strip()
        if name.lower() in existing:
            skipped += 1
            continue

        # Sector: first industry from comma-separated list
        industries = row.get("Organization Industries", "")
        sector = ""
        if pd.notna(industries):
            parts = [p.strip() for p in str(industries).split(",")]
            # Prefer SaaS/Software-sounding terms; otherwise take first
            priority = ["SaaS", "Software", "AI", "Cloud", "DevTools", "Developer"]
            sector = parts[0]
            for p in parts:
                if any(kw.lower() in p.lower() for kw in priority):
                    sector = p
                    break

        # Headcount
        headcount = row.get("Company Size")
        if pd.notna(headcount):
            try:
                headcount_str = str(int(float(headcount)))
            except (ValueError, TypeError):
                headcount_str = str(headcount)  # keep range strings like "10-50"
        else:
            headcount_str = None

        # Funding stage
        funding_stage = str(row["Funding Type"]) if pd.notna(row.get("Funding Type")) else None

        # Why interesting: use Comment if present
        comment = row.get("Comment")
        why_interesting = str(comment) if pd.notna(comment) else None

        # Website: Crunchbase URL is what we have; store it
        cb_url = row.get("Organization Name URL")
        website = str(cb_url) if pd.notna(cb_url) else None

        research_json = build_research_json(row)

        if dry_run:
            print(f"  WOULD INSERT: {name} | {funding_stage} | {sector} | {headcount_str} employees")
            inserted += 1
            existing.add(name.lower())
            continue

        con.execute(
            """
            INSERT INTO companies
                (name, website, sector, headcount_estimate, funding_stage,
                 why_interesting, source, date_added, status, research_json)
            VALUES (?, ?, ?, ?, ?, ?, 'crunchbase', ?, 'Watchlist', ?)
            """,
            (name, website, sector, headcount_str, funding_stage,
             why_interesting, today, research_json),
        )
        inserted += 1
        existing.add(name.lower())

    if not dry_run:
        con.commit()
    con.close()

    print(f"\n{'DRY RUN — ' if dry_run else ''}Done.")
    print(f"  Inserted: {inserted}")
    print(f"  Skipped (dupe or blank): {skipped}")
    print(f"  Total Y companies in sheet: {len(y)}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN — no writes to DB ===\n")
    seed(dry_run=dry_run)
