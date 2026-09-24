"""Single-writer architecture (app/background.py, app/main.py).

SQLite on a Modal Volume is last-writer-wins at the file level, so only the web
container may mount the Volume. These tests pin the pieces that make that safe:
the invariant itself, background jobs that dedupe and commit, and request
offloading so one container never freezes behind a slow handler.
"""
import ast
import asyncio
import re
import threading
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import SESSION_COOKIE, create_session_token
from app.background import BackgroundRunner, offload, run_capturing_output

MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"
ADMIN = Path(__file__).resolve().parent.parent / "app" / "admin.py"


def _functions_mounting_volume(path: Path = MAIN) -> list[str]:
    tree = ast.parse(path.read_text())
    names = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and any(k.arg == "volumes" for k in dec.keywords):
                names.append(node.name)
    return names


def test_only_web_mounts_the_volume():
    assert _functions_mounting_volume() == ["web"]


def test_admin_app_has_no_volume_and_no_web():
    # `modal run app/admin.py::x` must never start a second database writer.
    assert _functions_mounting_volume(ADMIN) == []
    src = ADMIN.read_text()
    assert "asgi_app" not in src and "recruiting-data" not in src


def test_admin_and_web_share_the_job_queue_names():
    def names(path):
        return sorted(re.findall(r'from_name\("(recruiting-engine-job[^"]+)"', path.read_text()))
    assert names(ADMIN) == names(MAIN) == ["recruiting-engine-job-results", "recruiting-engine-jobs"]


def test_every_admin_tool_has_a_web_side_job():
    tree = ast.parse(ADMIN.read_text())
    jobs = {c.args[0].value for c in ast.walk(tree)
            if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_relay"
            and isinstance(c.args[0], ast.Constant)}
    import app.main as main_module
    from app.background_jobs import JOBS
    missing = jobs - set(JOBS) - set(main_module.ADMIN_JOBS)
    assert not missing, f"admin tools with no job in the web container: {missing}"


def test_web_is_pinned_to_one_always_on_container():
    tree = ast.parse(MAIN.read_text())
    web = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "web")
    kw = {k.arg: ast.literal_eval(k.value) for d in web.decorator_list
          if isinstance(d, ast.Call) for k in d.keywords if k.arg in ("min_containers", "max_containers")}
    assert kw == {"min_containers": 1, "max_containers": 1}


def test_runner_dedupes_by_name_and_commits_after_each_job():
    commits = []
    runner = BackgroundRunner(commit_fn=lambda: commits.append(1))
    gate = threading.Event()

    first = runner.submit("scan", gate.wait, 5)
    assert first is not None
    assert runner.submit("scan", lambda: None) is None  # already running
    assert runner.is_running("scan")

    gate.set()
    first.result(timeout=5)
    assert commits == [1]
    assert runner.submit("scan", lambda: "again").result(timeout=5) == "again"
    assert commits == [1, 1]


def test_runner_commits_and_releases_the_name_when_a_job_fails():
    commits = []
    runner = BackgroundRunner(commit_fn=lambda: commits.append(1))

    def boom():
        raise RuntimeError("nope")

    fut = runner.submit("job", boom)
    try:
        fut.result(timeout=5)
        assert False, "expected the job error to surface on the future"
    except RuntimeError:
        pass
    assert commits == [1]
    assert not runner.is_running("job")


def _offload_app():
    async def slow(request):
        time.sleep(0.6)  # blocking, like SQLite or an LLM call inside async def
        return PlainTextResponse("slow")

    async def fast(request):
        return PlainTextResponse("fast")

    async def echo_form(request):
        form = await request.form()
        return PlainTextResponse(form.get("name", ""))

    return Starlette(routes=[
        Route("/slow", offload(slow)),
        Route("/fast", offload(fast)),
        Route("/form", offload(echo_form), methods=["POST"]),
    ])


def test_a_slow_request_does_not_block_a_fast_one():
    async def run():
        transport = httpx.ASGITransport(app=_offload_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t0 = time.monotonic()

            async def timed(path):
                r = await c.get(path)
                return r, time.monotonic() - t0

            slow_task = asyncio.create_task(timed("/slow"))
            await asyncio.sleep(0)  # let the slow request start first
            fast, fast_done = await timed("/fast")
            _, slow_done = await slow_task
        return fast, fast_done, slow_done

    fast, fast_done, slow_done = asyncio.run(run())
    assert fast.text == "fast"
    assert fast_done < 0.3 < slow_done, (
        f"fast finished at {fast_done:.2f}s, slow at {slow_done:.2f}s: fast waited behind slow")


def test_offloaded_route_still_reads_form_bodies():
    client = TestClient(_offload_app())
    assert client.post("/form", data={"name": "Acme"}).text == "Acme"


def test_run_scan_now_returns_immediately_and_dedupes(monkeypatch):
    from app.routes import create_app

    gate = threading.Event()
    monkeypatch.setattr("app.discovery.hunter.run_discovery_scan", lambda: gate.wait(5) and {})
    app = create_app()
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, create_session_token())
    headers = {"X-Requested-With": "XMLHttpRequest"}

    t0 = time.monotonic()
    first = client.post("/api/discovery/scan", headers=headers)
    assert time.monotonic() - t0 < 2
    assert "Scan started" in first.text
    assert "already running" in client.post("/api/discovery/scan", headers=headers).text
    gate.set()


def test_captured_output_goes_back_to_the_caller():
    def dry_run():
        print("id=140 WOULD STAMP 2026-07-01")
        return {"dry_run": True}

    result, output = run_capturing_output(dry_run)
    assert result == {"dry_run": True}
    assert "WOULD STAMP" in output


def test_a_failed_pool_submit_does_not_leave_the_name_stuck():
    runner = BackgroundRunner()
    runner._pool.shutdown()
    try:
        runner.submit("scan", lambda: None)
    except RuntimeError:
        pass
    assert not runner.is_running("scan")
