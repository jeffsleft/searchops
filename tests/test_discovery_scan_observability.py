"""
Tests the observability fix for run_discovery_scan_remote() (app/main.py).

Prior to this fix, run_discovery_scan()'s own "completed"/"partial" task_log
entry sits on the last line of that function — any uncaught exception mid-scan
skipped it entirely, leaving a "started" row with no way to distinguish a
crash from a genuine zero-yield day. Confirmed live: 6 of 11 daily scans since
2026-07-15 had no completion row. The fix wraps the single entry point both
the 6am UTC cron and manual `modal run` invocations go through.
"""
import app.main as main_module
from app.models import get_db


def test_crash_logs_failed_and_reraises(monkeypatch):
    def _boom():
        raise RuntimeError("simulated scan crash")

    monkeypatch.setattr("app.discovery.hunter.run_discovery_scan", _boom)

    try:
        main_module.run_discovery_scan_remote.local()
        assert False, "expected the crash to propagate"
    except RuntimeError as e:
        assert "simulated scan crash" in str(e)

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, message FROM task_log WHERE task_type = 'discovery_scan' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] == "failed"
    assert "simulated scan crash" in row["message"]


def test_success_path_unaffected(monkeypatch):
    with get_db() as conn:
        before = conn.execute(
            "SELECT COUNT(*) c FROM task_log WHERE task_type = 'discovery_scan'"
        ).fetchone()["c"]

    monkeypatch.setattr(
        "app.discovery.hunter.run_discovery_scan",
        lambda: {"scanned": 3, "new_found": 0, "errors": 0, "discovered_via_search": 0, "auto_scored": 0},
    )

    stats = main_module.run_discovery_scan_remote.local()
    assert stats["scanned"] == 3

    with get_db() as conn:
        after = conn.execute(
            "SELECT COUNT(*) c FROM task_log WHERE task_type = 'discovery_scan'"
        ).fetchone()["c"]
    # The mocked run_discovery_scan doesn't call log_task_event itself, so a
    # clean success path through the wrapper shouldn't add any new row.
    assert after == before
