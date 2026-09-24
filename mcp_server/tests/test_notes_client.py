"""Unit tests for notes_client.py — no network, no real Keychain access.

httpx.get/post are monkeypatched to return canned httpx.Response objects built
in-process (no server needed); subprocess.run is monkeypatched for the
Keychain lookup.
"""
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notes_client  # noqa: E402


# --- get_token ---------------------------------------------------------------

def test_get_token_success(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="sekret-token\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert notes_client.get_token() == "sekret-token"


def test_get_token_missing_entry_raises(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(44, args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(notes_client.KeychainTokenError, match="No Keychain entry"):
        notes_client.get_token()


def test_get_token_empty_value_raises(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="   \n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(notes_client.KeychainTokenError, match="is empty"):
        notes_client.get_token()


def test_get_token_no_security_binary_raises(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(notes_client.KeychainTokenError, match="macOS-only"):
        notes_client.get_token()


# --- NotesClient ---------------------------------------------------------

@pytest.fixture()
def client():
    return notes_client.NotesClient(base_url="https://example.test", token="fixed-token")


def test_find_target_sends_auth_and_csrf_headers(monkeypatch, client):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return httpx.Response(
            200,
            json={"status": "ok", "matches": [{"job_id": 1, "company": "Synthesia"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    matches = client.find_target("Synthesia")

    assert matches == [{"job_id": 1, "company": "Synthesia"}]
    assert captured["url"] == "https://example.test/api/notes/jobs/search"
    assert captured["params"] == {"q": "Synthesia"}
    assert captured["headers"]["Authorization"] == "Bearer fixed-token"
    assert captured["headers"]["X-Requested-With"] == "XMLHttpRequest"


def test_find_target_uses_keychain_when_no_token_given(monkeypatch):
    c = notes_client.NotesClient(base_url="https://example.test")
    monkeypatch.setattr(notes_client, "get_token", lambda: "from-keychain")

    def fake_get(url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer from-keychain"
        return httpx.Response(200, json={"status": "ok", "matches": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    c.find_target("anything")


def test_add_note_success(monkeypatch, client):
    def fake_post(url, json=None, headers=None, timeout=None):
        assert url == "https://example.test/api/notes/jobs/42/notes"
        assert json == {"text": "hello", "source": "claude-desktop"}
        return httpx.Response(200, json={"status": "success"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = client.add_note(42, "hello")
    assert result == {"status": "success"}


def test_add_note_not_found_returns_error_dict_not_raise(monkeypatch, client):
    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(404, json={"status": "error", "message": "Job not found"})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = client.add_note(999999, "hello")
    assert result == {"status": "error", "message": "Job not found"}


def test_add_note_too_long_returns_error_dict(monkeypatch, client):
    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(400, json={"status": "error", "message": "text exceeds 5000 chars"})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = client.add_note(1, "x" * 6000)
    assert result == {"status": "error", "message": "text exceeds 5000 chars"}


def test_recent_notes_returns_list(monkeypatch, client):
    def fake_get(url, params=None, headers=None, timeout=None):
        assert url == "https://example.test/api/notes/jobs/7/notes"
        assert params == {"limit": 5}
        return httpx.Response(
            200,
            json={"status": "ok", "notes": [{"text": "a"}, {"text": "b"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    notes = client.recent_notes(7)
    assert notes == [{"text": "a"}, {"text": "b"}]


def test_recent_notes_unknown_job_returns_empty_list(monkeypatch, client):
    def fake_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(404, json={"status": "error", "message": "Job not found"})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert client.recent_notes(999999) == []
