"""
Regression coverage for the Workday ATS client (app/discovery/ats_clients.py).

Added 2026-08-21 as part of "widen the hunt" — several hunt-target
companies (Genesys, and others noted "own-system (Workday)" in
hunt_targets.yaml) sit on Workday tenants, which previously fell through to
the generic BeautifulSoup scraper (no JD text, shallow link scrape).
Workday exposes a plain public JSON API — no auth, no JS rendering — at
{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}, confirmed live
against Genesys's real board. Same two-call shape as Ashby: a list/search
call for briefs, then a per-posting detail call for full JD text.
"""
import httpx

from app.discovery.ats_clients import detect_ats, fetch_workday_jobs


def test_detect_ats_workday_simple_url():
    ats_type, handle = detect_ats("https://genesys.wd1.myworkdayjobs.com/Genesys")
    assert ats_type == "workday"
    assert handle == "genesys|1|genesys"


def test_detect_ats_workday_with_locale_segment():
    ats_type, handle = detect_ats("https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers")
    assert ats_type == "workday"
    assert handle == "acme|5|acmecareers"


def test_fetch_workday_jobs_uses_two_call_shape(monkeypatch):
    list_response = {
        "total": 2,
        "jobPostings": [
            {"title": "RevOps Manager", "externalPath": "/job/Remote/RevOps-Manager_JR1",
             "locationsText": "Remote", "postedOn": "Posted Today"},
            {"title": "Software Engineer", "externalPath": "/job/NYC/Software-Engineer_JR2",
             "locationsText": "NYC", "postedOn": "Posted Today"},
        ],
    }
    detail_bodies = {
        "/job/Remote/RevOps-Manager_JR1": {"jobPostingInfo": {"jobDescription": "<p>RevOps JD</p>"}},
        "/job/NYC/Software-Engineer_JR2": {"jobPostingInfo": {"jobDescription": "<p>Eng JD</p>"}},
    }

    def _post(url, json=None, timeout=None):
        return httpx.Response(200, json=list_response, request=httpx.Request("POST", url))

    def _get(url, timeout=None):
        for path, body in detail_bodies.items():
            if url.endswith(path):
                return httpx.Response(200, json=body, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    jobs = fetch_workday_jobs("genesys|1|Genesys")

    assert len(jobs) == 2
    assert jobs[0]["title"] == "RevOps Manager"
    assert jobs[0]["description"] == "<p>RevOps JD</p>"
    assert jobs[0]["url"] == "https://genesys.wd1.myworkdayjobs.com/Genesys/job/Remote/RevOps-Manager_JR1"
    assert jobs[1]["description"] == "<p>Eng JD</p>"


def test_fetch_workday_jobs_degrades_gracefully_on_detail_failure(monkeypatch):
    """A single posting's detail fetch failing shouldn't drop the posting —
    it should still surface with an empty description rather than losing
    the whole company's scan (same contract as the Ashby client)."""
    list_response = {
        "total": 1,
        "jobPostings": [
            {"title": "Director of Ops", "externalPath": "/job/Remote/Director_JR9",
             "locationsText": "Remote", "postedOn": "Posted Today"},
        ],
    }

    def _post(url, json=None, timeout=None):
        return httpx.Response(200, json=list_response, request=httpx.Request("POST", url))

    def _get(url, timeout=None):
        return httpx.Response(500, text="server error", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    jobs = fetch_workday_jobs("genesys|1|Genesys")

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Director of Ops"
    assert jobs[0]["description"] == ""


def test_fetch_workday_jobs_paginates_until_total_reached(monkeypatch):
    """Two pages of 50 (PAGE_SIZE) should be fetched for a 60-posting board."""
    page_1 = {
        "total": 60,
        "jobPostings": [
            {"title": f"Role {i}", "externalPath": f"/job/Remote/Role-{i}_JR{i}",
             "locationsText": "Remote", "postedOn": "Posted Today"}
            for i in range(50)
        ],
    }
    page_2 = {
        "total": 60,
        "jobPostings": [
            {"title": f"Role {i}", "externalPath": f"/job/Remote/Role-{i}_JR{i}",
             "locationsText": "Remote", "postedOn": "Posted Today"}
            for i in range(50, 60)
        ],
    }
    calls = {"n": 0}

    def _post(url, json=None, timeout=None):
        calls["n"] += 1
        body = page_1 if calls["n"] == 1 else page_2
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    def _get(url, timeout=None):
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "x"}},
                               request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    jobs = fetch_workday_jobs("genesys|1|Genesys")

    assert calls["n"] == 2
    assert len(jobs) == 60


def test_fetch_workday_jobs_malformed_handle_returns_empty():
    assert fetch_workday_jobs("not-a-valid-handle") == []
