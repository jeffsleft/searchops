"""
Repeatable browser-based visual & integration audit using Playwright.
Visits login, completes authentication, and captures screenshots of key sections.
Ensures local server is running on http://localhost:8000.
"""
import os
import sys
import socket
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).parent.parent
SCREENSHOT_DIR = ROOT / "data" / "audit_screenshots"

def load_env():
    """Load environment variables from .env file, similar to run_local.py"""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

def is_server_running(host="localhost", port=8000):
    """Check if the local development server is active."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0

async def run_audit():
    load_env()
    
    # 1. Verify Local Server
    if not is_server_running():
        print("🚨 Error: The local server is not running on http://localhost:8000.")
        print("👉 Please run 'python run_local.py' in a separate terminal before running this audit.")
        sys.exit(1)
        
    password = os.environ.get("APP_PASSWORD")
    if not password:
        print("🚨 Error: APP_PASSWORD is not set in your .env file or environment.")
        sys.exit(1)

    print("🚀 Starting browser-based visual audit...")
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Launch Chromium (use headless=True for background running, change to False to watch it run)
        browser = await p.chromium.launch(headless=True)
        # Create a browser context with standard desktop viewport
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        # Listen to console messages and page errors
        page.on("console", lambda msg: print(f"📺 [Console {msg.type}] {msg.text}"))
        page.on("pageerror", lambda exc: print(f"🚨 [Page Error] {exc}"))
        page.on("response", lambda resp: print(f"📥 [HTTP {resp.status}] {resp.url}") if resp.status >= 400 else None)
        
        # --- LOGIN ---
        print("\n🔑 Logging in...")
        await page.goto("http://localhost:8000/login")
        await page.fill('input[name="password"]', password)
        
        # Take a screenshot before clicking Sign In
        await page.screenshot(path=str(SCREENSHOT_DIR / "01_login_page.png"))
        
        await page.click('button[type="submit"]')
        await page.wait_for_url("http://localhost:8000/")
        print("✅ Logged in successfully!")

        # --- DASHBOARD (NEW TAB) ---
        print("\n📊 Auditing Dashboard...")
        await page.wait_for_timeout(1000)  # Wait for CSS/layouts to settle
        await page.screenshot(path=str(SCREENSHOT_DIR / "02_dashboard_new.png"))
        
        # Toggle to "All" tab if it exists
        all_tab = page.locator("a:has-text('All')")
        if await all_tab.count() > 0:
            await all_tab.first.click()
            await page.wait_for_timeout(1000)
            await page.screenshot(path=str(SCREENSHOT_DIR / "03_dashboard_all.png"))

        # Open the first job detail drawer/panel if any exist
        job_links = page.locator("a[href^='/job/']")
        if await job_links.count() > 0:
            print("👉 Opening a Job detail page...")
            first_job_url = await job_links.first.get_attribute("href")
            await page.goto(f"http://localhost:8000{first_job_url}")
            await page.wait_for_timeout(1500)
            await page.screenshot(path=str(SCREENSHOT_DIR / "04_job_detail.png"))

        # --- PIPELINE KANBAN ---
        print("\n🛣️ Auditing Pipeline Kanban...")
        await page.goto("http://localhost:8000/pipeline")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(SCREENSHOT_DIR / "05_pipeline_kanban.png"))

        # --- DISCOVERED ---
        print("\n🛰️ Auditing Discovered Jobs...")
        await page.goto("http://localhost:8000/discovered")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(SCREENSHOT_DIR / "06_discovered.png"))

        # --- INTERVIEW PREP ---
        print("\n💼 Auditing Interview Prep...")
        await page.goto("http://localhost:8000/prep")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(SCREENSHOT_DIR / "07_interview_prep.png"))

        # --- OUTREACH TARGETS ---
        print("\n📣 Auditing Outreach Targets...")
        await page.goto("http://localhost:8000/companies")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(SCREENSHOT_DIR / "08_outreach_targets.png"))

        # --- HUNT TARGETS ---
        print("\n🎯 Auditing Hunt Targets...")
        await page.goto("http://localhost:8000/targets")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(SCREENSHOT_DIR / "08b_hunt_targets.png"))

        # --- VETTING ---
        print("\n⚖️ Auditing Vetting...")
        await page.goto("http://localhost:8000/vetting")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(SCREENSHOT_DIR / "09_vetting.png"))

        # --- SETTINGS ---
        print("\n⚙️ Auditing Settings...")
        await page.goto("http://localhost:8000/settings")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(SCREENSHOT_DIR / "10_settings.png"))

        # Clean close
        await browser.close()
        
    print(f"\n🎨 Visual audit complete! Screenshots saved in:")
    print(f"👉 {SCREENSHOT_DIR}")

if __name__ == "__main__":
    asyncio.run(run_audit())
