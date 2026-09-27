"""DEMO_MODE: the public portfolio demo is login-free, read-only, AI-free,
unindexed, and only ever loads the fictional example profile and corpus."""
import pytest
from starlette.testclient import TestClient

import app.config as config
from app.routes import create_app


@pytest.fixture()
def demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    return TestClient(create_app())  # note: no session cookie


def test_pages_open_without_login(demo):
    r = demo.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "fictional candidate and roles" in r.text
    assert demo.get("/login", follow_redirects=False).headers["location"] == "/"


def test_every_write_is_refused_with_a_reason(demo):
    for path in ("/job/1/stage", "/job/score", "/job/1/notes", "/api/discovery/scan"):
        r = demo.post(path, data={"x": "1"}, headers={"X-Requested-With": "XMLHttpRequest"})
        assert r.status_code == 403, path
        assert r.headers["X-Save-Status"] == "failed"


def test_demo_is_hidden_from_search_engines(demo):
    assert demo.get("/").headers["X-Robots-Tag"] == "noindex, nofollow"
    assert "Disallow: /" in demo.get("/robots.txt").text


def test_no_ai_calls_in_demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    from app.providers import get_provider
    with pytest.raises(RuntimeError, match="turned off in the demo"):
        get_provider()


def test_demo_loads_only_the_fictional_profile_and_corpus(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    from app.scoring.corpus import EXAMPLE_INVENTORY_PATH, resolve_inventory_path
    assert resolve_inventory_path() == (EXAMPLE_INVENTORY_PATH, True)
    profile = config._load_yaml_profile()
    assert "Alex Rivera" in str(profile), "demo must load candidate_profile.example.yaml"
    assert profile["compensation"]["base_min"] == 160000


def test_personal_ops_pages_are_hidden_from_the_demo_nav(demo):
    html = demo.get("/").text
    for hidden in ('href="/recruiters"', 'href="/prep"', 'href="/settings/health"', 'href="/guide"'):
        assert hidden not in html, hidden
    assert 'href="/settings/methodology"' in html


def test_production_still_requires_login():
    c = TestClient(create_app())
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"


def test_resume_and_cover_letter_never_render_in_the_demo(demo):
    for path in ("/job/1/resume", "/job/1/resume/download", "/job/1/cover-letter", "/job/1/cover-letter/download"):
        r = demo.get(path)
        assert r.status_code == 404, path
        assert "Not part of the demo" in r.text
        assert "Beaumont" not in r.text


def test_demo_ignores_a_stored_profile_row(monkeypatch):
    import json
    from app.models import get_db
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO candidate_settings (id, profile_json) VALUES (1, ?)",
                     (json.dumps({"candidate": {"name": "Real Person"}, "compensation": {"base_min": 999}}),))
    monkeypatch.setattr(config, "DEMO_MODE", True)
    profile = config.load_profile()
    assert "Real Person" not in str(profile) and "Alex Rivera" in str(profile)
