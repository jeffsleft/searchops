"""
Regression test for a real bug found via manual QA (2026-07-29): targets_research()
called the blocking, synchronous generate_gap_hypothesis() directly inside its
async _run() coroutine instead of via asyncio.to_thread(). Since nothing yielded
control back to the event loop, the call blocked the whole request -- the route's
own fire-and-forget "Researching..." response never actually got sent until the
blocking call finished, so a slow research call (Gemini retries, LLM latency)
made the request itself hang and eventually 408 instead of returning instantly.
Confirmed live: POST /targets/{id}/research took 109s and returned a 408.

Fix: wrap the call in asyncio.to_thread(), matching the identical pattern
already used correctly by company_trigger_research two hundred lines above.

Note on approach: this is a static source check, not a timing-based test.
Starlette's TestClient waits for the whole event loop to drain before a
request returns -- confirmed empirically that even the already-correct
asyncio.create_task(asyncio.to_thread(...)) pattern blocks for the full
duration of the background call under TestClient, so wall-clock timing can't
distinguish the buggy pattern from the fixed one in this harness. Only a real
ASGI server (uvicorn/Modal) shows the actual non-blocking behavior, which was
verified live. A static check is the reliable regression guard here.
"""
import inspect

import pytest
from starlette.testclient import TestClient

from app.models import get_db
from app.routes import create_app, targets_research
from app.auth import create_session_token, SESSION_COOKIE

HX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def test_targets_research_wraps_the_blocking_call_in_to_thread():
    source = inspect.getsource(targets_research)
    assert "asyncio.to_thread(" in source, (
        "generate_gap_hypothesis (or its replacement) must be called via "
        "asyncio.to_thread inside targets_research — calling it directly "
        "blocks the whole event loop for the duration of the research pipeline."
    )


@pytest.fixture()
def client():
    app = create_app()
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, create_session_token())
    return c


@pytest.fixture()
def company_id():
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO companies (name, tier_a, hunt_enabled, date_added) VALUES (?, 1, 1, date('now'))",
            ("Acme Testco",),
        )
        return cur.lastrowid


def test_research_route_fires_the_pipeline_and_returns_the_placeholder(
    client, company_id, monkeypatch
):
    calls = []

    def _fake_gap_hypothesis(co_id, force_research=False, force_metadata=False):
        calls.append((co_id, force_research, force_metadata))
        return {}

    monkeypatch.setattr(
        "app.services.research_service.generate_gap_hypothesis", _fake_gap_hypothesis
    )

    resp = client.post(f"/targets/{company_id}/research", headers=HX_HEADERS)

    assert resp.status_code == 200
    assert "Researching" in resp.text
    assert calls == [(company_id, True, False)]
