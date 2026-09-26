"""close_dead_listings: close scan-found roles that left the job board, safely."""
import pytest

from app.models import get_db


def _setup(conn, url, stage="discovered", source="hunter", age="-3 days", ats=("greenhouse", "acme")):
    co = conn.execute("SELECT id FROM companies WHERE name='DeadListCo'").fetchone()
    co_id = co[0] if co else conn.execute(
        "INSERT INTO companies (name, ats_type, ats_handle, hunt_enabled, date_added) VALUES ('DeadListCo', ?, ?, 1, date('now'))",
        ats).lastrowid
    return conn.execute(
        "INSERT INTO jobs (company, company_id, job_title, url, pipeline_stage, discovery_source, date_found) "
        "VALUES ('DeadListCo', ?, 'RevOps', ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now', ?))",
        (co_id, url, stage, source, age)).lastrowid


@pytest.fixture(autouse=True)
def _clean():
    with get_db() as conn:
        for t in ("pipeline_history", "application_outcomes"):
            conn.execute(f"DELETE FROM {t} WHERE job_id IN (SELECT id FROM jobs WHERE company='DeadListCo')")
        conn.execute("DELETE FROM jobs WHERE company='DeadListCo'")
        conn.execute("DELETE FROM companies WHERE name='DeadListCo'")


def _stage(job_id):
    with get_db() as conn:
        return conn.execute("SELECT pipeline_stage FROM jobs WHERE id=?", (job_id,)).fetchone()[0]


def test_closes_only_roles_gone_from_the_board(monkeypatch):
    with get_db() as conn:
        live = _setup(conn, "https://boards.greenhouse.io/acme/jobs/1")
        gone = _setup(conn, "https://boards.greenhouse.io/acme/jobs/2")
        mine = _setup(conn, "https://boards.greenhouse.io/acme/jobs/3", source=None)        # added by hand
        fresh = _setup(conn, "https://boards.greenhouse.io/acme/jobs/4", age="-1 hours")    # too new
        applied = _setup(conn, "https://boards.greenhouse.io/acme/jobs/5", stage="applied")  # past the inbox
    monkeypatch.setattr("app.discovery.ats_clients.list_job_urls",
                        lambda t, h: {"https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/9"})
    from app.background_jobs import close_dead_listings
    close_dead_listings()
    assert _stage(gone) == "job_listing_closed"
    assert [_stage(j) for j in (live, mine, fresh, applied)] == ["discovered", "discovered", "discovered", "applied"]


def test_an_unlistable_board_closes_nothing(monkeypatch):
    with get_db() as conn:
        job = _setup(conn, "https://boards.greenhouse.io/acme/jobs/2")
    monkeypatch.setattr("app.discovery.ats_clients.list_job_urls", lambda t, h: None)
    from app.background_jobs import close_dead_listings
    assert close_dead_listings()["closed"] == 0
    assert _stage(job) == "discovered"


def test_urls_on_a_different_host_are_not_judged(monkeypatch):
    # The company moved its job URLs to a new domain: nothing on the old host matches,
    # which must not read as "every role closed".
    with get_db() as conn:
        job = _setup(conn, "https://boards.greenhouse.io/acme/jobs/2")
    monkeypatch.setattr("app.discovery.ats_clients.list_job_urls",
                        lambda t, h: {"https://careers.acme.com/job/?gh_jid=77"})
    from app.background_jobs import close_dead_listings
    close_dead_listings()
    assert _stage(job) == "discovered"


def test_dry_run_changes_nothing(monkeypatch):
    with get_db() as conn:
        job = _setup(conn, "https://boards.greenhouse.io/acme/jobs/2")
    monkeypatch.setattr("app.discovery.ats_clients.list_job_urls",
                        lambda t, h: {"https://boards.greenhouse.io/acme/jobs/1"})
    from app.background_jobs import close_dead_listings
    assert close_dead_listings(dry_run=True)["closed"] == 1
    assert _stage(job) == "discovered"


def test_a_renamed_board_on_a_shared_host_is_not_judged(monkeypatch):
    # Greenhouse/Lever/Ashby share one host across companies. Old URLs under the
    # previous board name must not read as closed.
    with get_db() as conn:
        job = _setup(conn, "https://boards.greenhouse.io/acme/jobs/2")
    monkeypatch.setattr("app.discovery.ats_clients.list_job_urls",
                        lambda t, h: {"https://boards.greenhouse.io/acmenew/jobs/9"})
    from app.background_jobs import close_dead_listings
    close_dead_listings()
    assert _stage(job) == "discovered"


def test_a_job_moved_on_during_the_check_is_not_closed(monkeypatch):
    with get_db() as conn:
        job = _setup(conn, "https://boards.greenhouse.io/acme/jobs/2")

    def listing(t, h):
        with get_db() as conn:  # Jeff applies while boards are being listed
            conn.execute("UPDATE jobs SET pipeline_stage='applied' WHERE id=?", (job,))
        return {"https://boards.greenhouse.io/acme/jobs/1"}
    monkeypatch.setattr("app.discovery.ats_clients.list_job_urls", listing)
    from app.background_jobs import close_dead_listings
    close_dead_listings()
    assert _stage(job) == "applied"
