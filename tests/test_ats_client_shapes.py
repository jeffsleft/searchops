"""Job-board feed shapes that crashed a whole board's scan on 2026-09-24."""
import app.discovery.ats_clients as ats


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_html_to_text_handles_escaped_and_raw_html():
    assert ats.html_to_text("&lt;p&gt;Lead &lt;b&gt;RevOps&lt;/b&gt;&lt;/p&gt;") == "Lead RevOps"
    assert ats.html_to_text("<ul><li>SQL</li><li>Salesforce</li></ul>") == "SQL Salesforce"
    assert ats.html_to_text("") == ""


def test_lever_plain_string_description_body_does_not_crash(monkeypatch):
    posting = {"text": "RevOps Lead", "hostedUrl": "https://jobs.lever.co/x/1",
               "descriptionBody": "a plain string on some boards",
               "descriptionPlain": "Own the revenue engine.",
               "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}],
               "additionalPlain": "Remote US.", "categories": {"location": "Remote"}}
    monkeypatch.setattr(ats.httpx, "get", lambda *a, **k: _Resp([posting]))
    jobs = ats.fetch_lever_jobs("tinybird")
    assert len(jobs) == 1
    assert "Own the revenue engine." in jobs[0]["description"]
    assert "Requirements: SQL" in jobs[0]["description"]


def test_lever_error_object_returns_no_jobs(monkeypatch):
    monkeypatch.setattr(ats.httpx, "get", lambda *a, **k: _Resp({"ok": False, "error": "Document not found"}))
    assert ats.fetch_lever_jobs("nope") == []


def test_ashby_unknown_board_returns_no_jobs(monkeypatch):
    monkeypatch.setattr(ats.httpx, "post", lambda *a, **k: _Resp({"data": {"jobBoard": None}}))
    assert ats.fetch_ashby_jobs("lindy") == []


def test_detects_smartrecruiters_and_teamtailor():
    assert ats.detect_ats("https://jobs.smartrecruiters.com/ServiceNow") == ("smartrecruiters", "servicenow")
    assert ats.detect_ats("https://careers.lindy.ai/jobs.rss") == ("teamtailor", "https://careers.lindy.ai")
    assert ats.detect_ats("https://acme.teamtailor.com/jobs") == ("teamtailor", "https://acme.teamtailor.com")
    assert ats.detect_ats("https://jobs.ashbyhq.com/claylabs") == ("ashby", "claylabs")


def test_teamtailor_rss_parses_every_item(monkeypatch):
    rss = b"""<?xml version="1.0"?><rss><channel>
      <item><title>RevOps Lead</title><link>https://careers.x.ai/jobs/1-revops</link>
            <description>&lt;p&gt;Own the funnel&lt;/p&gt;</description><pubDate>Mon, 01 Sep 2026</pubDate></item>
      <item><title>Engineer</title><link>https://careers.x.ai/jobs/2-eng</link><description>x</description></item>
    </channel></rss>"""
    class R:
        content = rss
        def raise_for_status(self): pass
    monkeypatch.setattr(ats.httpx, "get", lambda *a, **k: R())
    monkeypatch.setattr(ats, "validate_url", lambda u: None)  # fake domain; skip the DNS-based guard
    jobs = ats.fetch_teamtailor_jobs("https://careers.x.ai")
    assert [j["title"] for j in jobs] == ["RevOps Lead", "Engineer"]
    assert ats.html_to_text(jobs[0]["description"]) == "Own the funnel"


def test_smartrecruiters_fetches_details_only_for_wanted_titles(monkeypatch):
    calls = []
    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/postings"):
            return _Resp({"totalFound": 2, "content": [
                {"id": "1", "name": "Director Sales Operations", "location": {"city": "Remote"}},
                {"id": "2", "name": "Software Engineer", "location": {}}]})
        return _Resp({"jobAd": {"sections": {"jobDescription": {"title": "About", "text": "<p>Run deal desk</p>"}}}})
    monkeypatch.setattr(ats.httpx, "get", fake_get)
    monkeypatch.setattr(ats.time, "sleep", lambda s: None)
    jobs = ats.fetch_smartrecruiters_jobs("servicenow", want=lambda t: "operations" in t.lower())
    assert [j["title"] for j in jobs] == ["Director Sales Operations"]
    assert "Run deal desk" in jobs[0]["description"]
    assert sum(u.endswith("/1") for u in calls) == 1 and not any(u.endswith("/2") for u in calls)
