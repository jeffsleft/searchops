import os
import sys
import yaml
from datetime import date
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from app.models import get_db, init_db
from app.discovery.ats_clients import detect_ats

CONFIG_PATH = Path(__file__).parent.parent / "app" / "discovery" / "hunt_targets.yaml"

def seed_targets():
    if not CONFIG_PATH.exists():
        print(f"Config not found at {CONFIG_PATH}")
        return

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    init_db()
    today = str(date.today())
    count = 0

    with get_db() as conn:
        for group in config.get("tracked_companies", []):
            category = group.get("category", "Unknown")
            for company in group.get("companies", []):
                name = company.get("name")
                url = company.get("url")
                if not name or not url:
                    continue

                # Detect ATS
                ats_type, ats_handle = detect_ats(url)

                # Upsert into companies
                existing = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE companies SET
                           careers_url = ?, ats_type = ?, ats_handle = ?,
                           sector = ?, hunt_enabled = 1, updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (url, ats_type, ats_handle, category, existing["id"])
                    )
                else:
                    conn.execute(
                        """INSERT INTO companies
                           (name, careers_url, ats_type, ats_handle, sector, hunt_enabled, date_added, source, status)
                           VALUES (?, ?, ?, ?, ?, 1, ?, 'hunter_seed', 'Watchlist')""",
                        (name, url, ats_type, ats_handle, category, today)
                    )
                count += 1
                print(f"Seeded: {name} ({ats_type})")

    print(f"Successfully seeded {count} companies.")

if __name__ == "__main__":
    seed_targets()
