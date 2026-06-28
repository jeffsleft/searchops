"""
Database backup module. Snapshots the SQLite DB to local backups folder.
WAL-aware: uses VACUUM INTO to produce a single clean DB file.
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from app.config import DATABASE_PATH

BACKUP_DIR = Path(DATABASE_PATH).parent / "backups"
KEEP = 8


def backup_database() -> None:
    """
    Weekly snapshot:
    1. Create /data/backups/<recruiting-YYYYMMDD-HHMMSS.db> via VACUUM INTO
    2. Rotate local backups, keep last 8
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    snapshot_filename = f"recruiting-{now_utc.strftime('%Y%m%d-%H%M%S')}.db"
    snapshot_path = BACKUP_DIR / snapshot_filename

    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.execute("VACUUM INTO ?", (str(snapshot_path),))
        conn.close()
        snapshot_bytes = snapshot_path.stat().st_size
        print(f"[db_backup] Local snapshot created: {snapshot_filename} ({snapshot_bytes} bytes)")
    except Exception as e:
        print(f"[db_backup] VACUUM INTO failed: {e}")
        from app.notifications.slack import _post
        _post({"text": f":warning: DB backup failed: VACUUM INTO error: {str(e)[:200]}"})
        raise

    _rotate_local_backups(BACKUP_DIR)
    print(f"[db_backup] Success: {snapshot_filename}")


def _rotate_local_backups(backup_dir: Path) -> None:
    """Keep only the newest KEEP backups; delete older ones."""
    files = sorted(backup_dir.glob("recruiting-*.db"))
    if len(files) > KEEP:
        for f in files[:-KEEP]:
            try:
                f.unlink()
                print(f"[db_backup] Rotated (deleted): {f.name}")
            except Exception as e:
                print(f"[db_backup] Failed to delete {f.name}: {e}")


if __name__ == "__main__":
    backup_database()
