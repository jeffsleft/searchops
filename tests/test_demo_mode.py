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
    for hidden in ('href="/recruiters"', 'href="/prep"', 'href="/companies"', 'href="/guide"',
                   'href="/admin/patterns"', 'href="/settings/interviews"'):
        assert hidden not in html, hidden
    for shown in ('href="/settings/methodology"', 'href="/targets"', 'href="/settings"',
                  'href="/settings/progress"', 'href="/settings/health"'):
        assert shown in html, shown
    assert "Score a Job" not in html


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


def test_demo_seed_builds_the_fictional_pipeline(monkeypatch, tmp_path):
    import app.models as models
    db = str(tmp_path / "demo.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db)
    monkeypatch.setattr(models, "DATABASE_PATH", db)
    from app.demo_seed import seed_demo_db
    assert seed_demo_db() == 11
    assert seed_demo_db() == 0  # idempotent on restart
    with models.get_db() as conn:
        stages = dict(conn.execute("SELECT company, pipeline_stage FROM jobs").fetchall())
        rejected = conn.execute("SELECT COUNT(*) FROM jobs WHERE auto_rejected = 1").fetchone()[0]
    assert stages["Sentora"] == "hm_interview" and stages["Kettle & Co."] == "dismissed"
    assert rejected == 2


def test_demo_image_ships_no_personal_files():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "demo_app.py").read_text()
    shipped = re.findall(r'add_local_(?:file|dir)\(\s*"([^"]+)"', src)
    assert shipped, "expected the image to list what it ships"
    for path in shipped:
        assert path in ("app/static", "app/templates", "app/voice/constraints", "demo_data",
                        "candidate_profile.example.yaml", "data/Accomplishments_Inventory.example.docx"), path
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#", "-")))
    for forbidden in ("volumes=", "Secret.from_name", "recruiting-secrets", "notes-api-token"):
        assert forbidden not in code, forbidden


def test_seeded_demo_pages_are_fictional_and_clean(monkeypatch, tmp_path):
    import app.models as models
    db = str(tmp_path / "demo.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db)
    monkeypatch.setattr(models, "DATABASE_PATH", db)
    monkeypatch.setattr(config, "DEMO_MODE", True)
    from app.demo_seed import seed_demo_db
    from app.services.metrics_service import integrity_checks
    seed_demo_db()
    client = TestClient(create_app())
    banner = "This is the tool Jeff Beaumont built"
    for path in ("/", "/pipeline", "/discovered", "/targets", "/settings",
                 "/settings/progress", "/settings/health", "/settings/methodology"):
        r = client.get(path)
        assert r.status_code == 200, path
        body = r.text.replace(banner, "")
        for real in ("Beaumont", "Jeff", "GitLab", "Mercy Ships", "Auburn"):
            assert real not in body, (path, real)
    assert "Forgeline" in client.get("/targets").text
    assert "Last scan never" not in client.get("/").text
    # The seeded history is internally consistent: Health shows no integrity warnings.
    assert all(c["count"] == 0 for c in integrity_checks()["checks"])
