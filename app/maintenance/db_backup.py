"""
Database backup module. Snapshots the SQLite DB to local backups folder and Google Drive.
WAL-aware: uses VACUUM INTO to produce a single clean DB file.
"""
import os
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
    3. Upload to Google Drive folder "Recruiting Engine DB Backups" (rotate last 8 there too)
    4. On Drive failure: Slack alert but preserve local snapshot
    5. On full success: print summary
    """
    try:
        # Ensure backup directory exists
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        # Compute timestamped filename in UTC
        now_utc = datetime.now(timezone.utc)
        snapshot_filename = f"recruiting-{now_utc.strftime('%Y%m%d-%H%M%S')}.db"
        snapshot_path = BACKUP_DIR / snapshot_filename

        # Snapshot via VACUUM INTO (WAL-aware, produces single clean file)
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

        # Rotate local backups — keep newest 8
        _rotate_local_backups(BACKUP_DIR)

        # Upload to Google Drive
        try:
            _upload_to_drive(snapshot_path, snapshot_filename)
        except Exception as e:
            # Drive upload failure: alert but do NOT delete the local snapshot
            print(f"[db_backup] Google Drive upload failed: {e}")
            from app.notifications.slack import _post
            _post({"text": f":warning: DB backup to Google Drive failed: {str(e)[:200]} (local snapshot preserved)"})
            raise

        print(f"[db_backup] Success: {snapshot_filename} backed up locally and to Drive")

    except Exception:
        # Log the exception (already posted to Slack above)
        raise


def _rotate_local_backups(backup_dir: Path) -> None:
    """Keep only the newest KEEP backups in the directory; delete older ones."""
    files = sorted(backup_dir.glob("recruiting-*.db"))
    if len(files) > KEEP:
        to_delete = files[:-KEEP]
        for f in to_delete:
            try:
                f.unlink()
                print(f"[db_backup] Rotated (deleted): {f.name}")
            except Exception as e:
                print(f"[db_backup] Failed to delete {f.name}: {e}")


def _upload_to_drive(snapshot_path: Path, snapshot_filename: str) -> None:
    """Upload snapshot to Google Drive, ensuring target folder exists."""
    from app.sheets.sync import _get_creds
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _get_creds()
    service = build('drive', 'v3', credentials=creds)

    # Find-or-create backup folder
    folder_name = os.environ.get("DB_BACKUP_DRIVE_FOLDER", "Recruiting Engine DB Backups")
    folder_id = _find_or_create_folder(service, folder_name)

    # Upload the snapshot file
    media = MediaFileUpload(str(snapshot_path), mimetype='application/x-sqlite3', resumable=True)
    file_body = {
        "name": snapshot_filename,
        "parents": [folder_id],
    }
    service.files().create(body=file_body, media_body=media, fields='id').execute()
    print(f"[db_backup] Uploaded {snapshot_filename} to Drive folder '{folder_name}'")

    # Rotate Drive backups — keep newest 8
    _rotate_drive_backups(service, folder_id)


def _find_or_create_folder(service, folder_name: str) -> str:
    """Find a Drive folder by name, or create it if missing. Return folder ID."""
    # Query for existing folder
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = results.get('files', [])

    if files:
        return files[0]['id']

    # Create folder
    folder_body = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    created = service.files().create(body=folder_body, fields='id').execute()
    print(f"[db_backup] Created Drive folder: {folder_name}")
    return created['id']


def _rotate_drive_backups(service, folder_id: str) -> None:
    """Keep only the newest KEEP files in the Drive folder; delete older ones."""
    # List files in the folder, sorted by name (timestamp format sorts lexically)
    query = f"'{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, orderBy='name', fields='files(id, name)').execute()
    files = results.get('files', [])

    # Keep newest KEEP files; delete the rest
    if len(files) > KEEP:
        to_delete = files[:-KEEP]
        for f in to_delete:
            try:
                service.files().delete(fileId=f['id']).execute()
                print(f"[db_backup] Rotated (deleted from Drive): {f['name']}")
            except Exception as e:
                print(f"[db_backup] Failed to delete {f['name']} from Drive: {e}")


if __name__ == "__main__":
    backup_database()
