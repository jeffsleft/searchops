"""
Recruiter CRM. Tracks warm relationships, last contact, stale alerts.
"""
from datetime import date
from app.models import get_db


def add_recruiter(
    name: str,
    firm: str = "",
    linkedin_url: str = "",
    email: str = "",
    phone: str = "",
    specialty: str = "",
    notes: str = "",
    relationship_status: str = "Cold",
    stale_alert_days: int = 30,
) -> int:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO recruiters
               (name, firm, linkedin_url, email, phone, specialty,
                notes, relationship_status, stale_alert_days)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (name, firm, linkedin_url, email, phone, specialty,
             notes, relationship_status, stale_alert_days),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def log_contact(recruiter_id: int, note: str = "") -> None:
    """Update last_contact_date and append note."""
    today = str(date.today())
    with get_db() as conn:
        if note:
            conn.execute(
                """UPDATE recruiters SET
                   last_contact_date = ?,
                   relationship_status = CASE WHEN relationship_status = 'Cold' THEN 'Warm' ELSE relationship_status END,
                   notes = COALESCE(notes, '') || '\n' || ? || ': ' || ?,
                   updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (today, today, note, recruiter_id),
            )
        else:
            conn.execute(
                """UPDATE recruiters SET
                   last_contact_date = ?,
                   updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (today, recruiter_id),
            )


def get_stale_recruiters() -> list[dict]:
    """Return recruiters whose last contact exceeds their stale_alert_days threshold."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT *, julianday('now') - julianday(last_contact_date) as days_since
               FROM recruiters
               WHERE last_contact_date IS NOT NULL
                 AND julianday('now') - julianday(last_contact_date) > stale_alert_days
               ORDER BY days_since DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_recruiters() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM recruiters ORDER BY relationship_status DESC, last_contact_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_recruiter(recruiter_id: int, **fields) -> None:
    allowed = {"name", "firm", "linkedin_url", "email", "phone", "specialty",
               "notes", "relationship_status", "stale_alert_days"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_db() as conn:
        conn.execute(
            f"UPDATE recruiters SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (*updates.values(), recruiter_id),
        )
