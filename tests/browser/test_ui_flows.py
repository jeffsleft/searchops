"""Real-browser tests for the save feedback and kanban drag (PRs #127-#129).

Boots the actual app (uvicorn) on a throwaway, seeded SQLite database and drives
Chromium through the flows that used to fail silently. Skips when Playwright or
its browser isn't installed; CI installs both.
"""
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "browser-test-password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_url():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    env = {**os.environ, "DATABASE_PATH": db, "APP_PASSWORD": PASSWORD,
           "SESSION_SECRET": "b" * 64, "GEMINI_API_KEY": "unused", "SLACK_WEBHOOK_URL": ""}
    # Schema first, then seed: one job on the board, one watched company.
    subprocess.run([sys.executable, "-c",
                    "import app.config as c, app.models as m, os; "
                    "c.DATABASE_PATH = m.DATABASE_PATH = os.environ['DATABASE_PATH']; m.init_db()"],
                   cwd=ROOT, env=env, check=True)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO jobs (id, company, job_title, pipeline_stage, final_score, auto_rejected, "
                 "ethics_vetted, date_found) VALUES (1, 'Acme', 'Director RevOps', 'researching', 7.5, 0, 1, date('now'))")
    conn.execute("INSERT INTO companies (id, name, hunt_enabled, careers_url, date_added) "
                 "VALUES (1, 'Acme Co', 1, 'https://boards.greenhouse.io/acme', date('now'))")
    conn.commit()
    conn.close()

    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.asgi:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.2)
    yield url, db
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def page(app_url):
    url, _ = app_url
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium unavailable: {e}")
        pg = browser.new_page()
        pg.goto(f"{url}/login")
        pg.fill("input[name=password]", PASSWORD)
        pg.click("button[type=submit]")
        pg.wait_for_url(f"{url}/")
        yield pg
        browser.close()


def _stage(db, job_id=1):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT pipeline_stage FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
    finally:
        conn.close()


DRAG = """(target) => {
  const card = document.querySelector('.kcard[data-job-id="1"]');
  const col = [...document.querySelectorAll('.kanban-col')]
      .find(c => c.querySelector('.kanban-col-body').dataset.stage === target);
  const dt = new DataTransfer();
  card.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
  col.querySelector('.kanban-col-head').dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt}));
  card.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
}"""


def test_drag_moves_the_card_saves_it_and_says_so(page, app_url):
    url, db = app_url
    page.goto(f"{url}/pipeline")
    page.evaluate(DRAG, "outreach")
    page.wait_for_selector("#stage-toast:has-text('Saved: moved to Outreach')")
    assert page.locator('.kanban-col-body[data-stage="outreach"] .kcard[data-job-id="1"]').count() == 1
    assert _stage(db) == "outreach"


def test_a_failed_save_puts_the_card_back_and_says_why(page, app_url):
    url, db = app_url
    page.goto(f"{url}/pipeline")
    page.route("**/job/1/stage", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"ok": false, "error": "Simulated failure."}'))
    page.evaluate(DRAG, "applied")
    page.wait_for_selector("#stage-toast:has-text('Not saved. Simulated failure.')")
    assert page.locator('.kanban-col-body[data-stage="outreach"] .kcard[data-job-id="1"]').count() == 1
    assert _stage(db) == "outreach"
    page.unroute("**/job/1/stage")


def test_notes_say_saved_or_not_saved(page, app_url):
    url, _ = app_url
    page.goto(f"{url}/job/1")
    form = page.locator('form[hx-post="/job/1/notes"]')
    form.locator("textarea").fill("   ")
    form.locator("textarea").evaluate("t => htmx.trigger(t.form, 'submit')")
    page.wait_for_selector("#stage-toast:has-text('Not saved')")
    assert page.locator("#job-notes-list").count() == 1, "a rejected note must not wipe the list"
    form.locator("textarea").fill("Called the recruiter")
    form.locator("textarea").evaluate("t => htmx.trigger(t.form, 'submit')")
    page.wait_for_selector("#stage-toast:has-text('Saved')")


def test_monitoring_toggle_flips_its_label(page, app_url):
    url, _ = app_url
    page.goto(f"{url}/targets")
    # Load the company's panel and show it (the list's row click does both).
    page.evaluate("document.getElementById('targets-panel').style.display = 'block'; "
                  "htmx.ajax('GET', '/targets/1/detail', {target: '#targets-panel', swap: 'innerHTML'})")
    toggle = page.locator('form[hx-post="/targets/1/toggle"] button')
    toggle.wait_for()
    before = toggle.inner_text().strip()
    toggle.click()
    page.wait_for_function(f"() => document.querySelector('form[hx-post=\"/targets/1/toggle\"] button')"
                           f".innerText.trim() !== {before!r}")
    page.wait_for_selector("#stage-toast:has-text('Saved')")
