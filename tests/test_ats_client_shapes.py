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
