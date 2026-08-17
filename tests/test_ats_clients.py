"""
Regression coverage for the Ashby ATS client (app/discovery/ats_clients.py).

Ashby's public jobBoardWithTeams query dropped isListed and
jobRequisition.description from its schema (~8/2026) without notice — the
old single-query fetch_ashby_jobs() errored on every call, was swallowed by
its own except block, and silently returned zero jobs for every Ashby
company in hunt_targets.yaml (~20 of them) for an unknown period. The fix
splits the fetch into a list query (briefs) + a per-posting detail query
(descriptionHtml). These tests pin that two-query contract so a future
Ashby schema change fails loudly here instead of degrading discovery yield
silently again.

Also covers detect_ats() handling of Ashby "hosted jobs page name" handles
that contain a space (e.g. "Trunk Tools", stored as .../Trunk%20Tools in
hunt_targets.yaml) — the prior regex silently truncated the handle at the
first non [a-z0-9_-] character.
"""
import httpx

from app.discovery.ats_clients import detect_ats, fetch_ashby_jobs


def _fake_post(list_response, detail_responses):
    """Return a monkeypatch-able stand-in for httpx.post.

    First call (list query) gets `list_response`; subsequent calls (detail
    queries) get one entry from `detail_responses` each, in order.
    """
    calls = {"n": 0}

    def _post(url, json=None, timeout=None):
        calls["n"] += 1
        body = list_response if calls["n"] == 1 else detail_responses[calls["n"] - 2]
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    return _post


def test_detect_ats_ashby_handles_space_in_handle():
    ats_type, handle = detect_ats("https://jobs.ashbyhq.com/Trunk%20Tools")
    assert ats_type == "ashby"
    assert handle == "trunk tools"


def test_detect_ats_ashby_simple_handle_unaffected():
    ats_type, handle = detect_ats("https://jobs.ashbyhq.com/eliseai")
    assert ats_type == "ashby"
    assert handle == "eliseai"


def test_fetch_ashby_jobs_uses_two_query_shape(monkeypatch):
    list_response = {
        "data": {
            "jobBoard": {
                "jobPostings": [
                    {"id": "job-1", "title": "GTM Strategy and Operations Manager",
                     "employmentType": "FullTime", "locationName": "Remote"},
                    {"id": "job-2", "title": "Software Engineer",
                     "employmentType": "FullTime", "locationName": "NYC"},
                ]
            }
        }
    }
    detail_responses = [
        {"data": {"jobPosting": {"descriptionHtml": "<p>GTM Ops JD text</p>"}}},
        {"data": {"jobPosting": {"descriptionHtml": "<p>Eng JD text</p>"}}},
    ]

    monkeypatch.setattr(httpx, "post", _fake_post(list_response, detail_responses))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    jobs = fetch_ashby_jobs("eliseai")

    assert len(jobs) == 2
    assert jobs[0]["title"] == "GTM Strategy and Operations Manager"
    assert jobs[0]["description"] == "<p>GTM Ops JD text</p>"
    assert jobs[0]["url"] == "https://jobs.ashbyhq.com/eliseai/job-1"
    assert jobs[1]["description"] == "<p>Eng JD text</p>"


def test_fetch_ashby_jobs_degrades_gracefully_on_detail_failure(monkeypatch):
    """A single posting's detail fetch failing (e.g. a 429) shouldn't drop
    the posting — it should still surface with an empty description rather
    than losing the whole company's scan."""
    list_response = {
        "data": {"jobBoard": {"jobPostings": [
            {"id": "job-1", "title": "RevOps Director", "employmentType": "FullTime", "locationName": "Remote"},
        ]}}
    }
    calls = {"n": 0}

    def _post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=list_response, request=httpx.Request("POST", url))
        return httpx.Response(429, text="rate limited", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    jobs = fetch_ashby_jobs("eliseai")

    assert len(jobs) == 1
    assert jobs[0]["title"] == "RevOps Director"
    assert jobs[0]["description"] == ""


def test_fetch_ashby_jobs_url_encodes_handle_with_space(monkeypatch):
    list_response = {
        "data": {"jobBoard": {"jobPostings": [
            {"id": "job-1", "title": "BDR", "employmentType": "FullTime", "locationName": "Austin"},
        ]}}
    }
    detail_response = {"data": {"jobPosting": {"descriptionHtml": "<p>JD</p>"}}}

    monkeypatch.setattr(httpx, "post", _fake_post(list_response, [detail_response]))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    jobs = fetch_ashby_jobs("trunk tools")

    assert jobs[0]["url"] == "https://jobs.ashbyhq.com/trunk%20tools/job-1"
