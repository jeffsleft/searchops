"""Run process_new_urls() locally with .env loaded. Use to verify sync before re-enabling scheduler."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

env_path = Path(__file__).parent.parent / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

from app.sheets.sync import process_new_urls

print("Running sync...")
results = process_new_urls()
print(f"Done. Processed {len(results)} rows.")
