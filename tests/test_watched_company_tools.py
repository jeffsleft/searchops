"""Admin tools that keep a watched company's careers_url and job-board type in step."""
import app.main as main
from app.models import get_db


def _company(name, url, ats_type=None, ats_handle=None):
    with get_db() as conn:
        conn.execute("DELETE FROM companies WHERE name = ?", (name,))
        conn.execute("INSERT INTO companies (name, careers_url, ats_type, ats_handle, hunt_enabled, tier_a, date_added) "
                     "VALUES (?, ?, ?, ?, 1, 1, date('now'))", (name, url, ats_type, ats_handle))


def _row(name):
    with get_db() as conn:
        return dict(conn.execute("SELECT careers_url, ats_type, ats_handle, hunt_enabled FROM companies WHERE name=?",
                                 (name,)).fetchone())


def test_redetect_fixes_a_url_whose_ats_type_drifted():
    _company("ClayTest", "https://jobs.ashbyhq.com/claylabs")  # ats_type never set
    assert "ClayTest" in main._redetect_ats_impl(dry_run=True)["changed"]
    assert _row("ClayTest")["ats_type"] is None  # dry run writes nothing
    main._redetect_ats_impl(dry_run=False)
    assert (_row("ClayTest")["ats_type"], _row("ClayTest")["ats_handle"]) == ("ashby", "claylabs")
    assert "ClayTest" not in main._redetect_ats_impl(dry_run=True)["changed"]


def test_set_watched_company_moves_board_and_can_pause():
    _company("SNTest", "https://www.servicenow.com/company/careers.html", "generic", "")
    main._set_watched_company_impl("SNTest", careers_url="https://jobs.smartrecruiters.com/ServiceNow", dry_run=False)
    r = _row("SNTest")
    assert (r["ats_type"], r["ats_handle"], r["hunt_enabled"]) == ("smartrecruiters", "servicenow", 1)
    main._set_watched_company_impl("SNTest", hunt_enabled=False, dry_run=False)
    assert _row("SNTest")["hunt_enabled"] == 0 and _row("SNTest")["ats_type"] == "smartrecruiters"


def test_stamp_careers_url_also_sets_the_board_type():
    _company("StampTest", None)
    main._stamp_careers_url_impl("StampTest", "https://jobs.lever.co/acme", dry_run=False)
    assert (_row("StampTest")["ats_type"], _row("StampTest")["ats_handle"]) == ("lever", "acme")


def test_fixing_a_url_leaves_a_paused_company_paused():
    _company("PausedTest", "https://old.example.com/careers", "generic", "")
    main._set_watched_company_impl("PausedTest", hunt_enabled=False, dry_run=False)
    main._set_watched_company_impl("PausedTest", careers_url="https://jobs.lever.co/acme", dry_run=False)
    assert _row("PausedTest")["hunt_enabled"] == 0
    assert _row("PausedTest")["ats_type"] == "lever"


def test_provisional_company_names_for_new_boards():
    from app.jobs.fetch import extract_provisional_company
    assert extract_provisional_company("https://acme-labs.teamtailor.com/jobs/1-revops") == "Acme Labs"
    assert extract_provisional_company("https://careers.lindy.ai/jobs.rss") == "Lindy"
    assert extract_provisional_company("https://visa.wd5.myworkdayjobs.com/Visa/job/123") == "Visa"
