"""GeminiProvider call shape and fallback (app/providers/gemini.py).

A fake client stands in for Google, so these pin how the app calls Gemini:
system prompt in system_instruction, per-model thinking level on JSON calls,
and falling through the model chain when one model is busy or out of quota.
"""
import pytest
from google.genai import errors

import app.providers.gemini as g


class _Resp:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, script):
        self.script = script  # model -> list of results (str or Exception), consumed in order
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)


def _provider(monkeypatch, script):
    fake = type("_C", (), {})()
    fake.models = _FakeModels(script)
    monkeypatch.setattr(g, "_get_client", lambda: fake)
    monkeypatch.setattr(g, "DEFAULT_PRO_MODEL", "gemini-flash-lite-latest")
    monkeypatch.setattr(g, "FALLBACK_MODELS", ["gemini-flash-lite-latest", "gemini-flash-latest"])
    return g.GeminiProvider(), fake.models


def _busy():
    return errors.ServerError(503, {"error": {"message": "high demand"}})


def _quota():
    return errors.ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})


def test_system_prompt_goes_in_system_instruction(monkeypatch):
    p, models = _provider(monkeypatch, {"gemini-flash-lite-latest": ['{"ok": true}']})
    p.generate_json("score this", system="You are a scorer.")
    call = models.calls[0]
    assert call["contents"] == "score this"
    assert call["config"].system_instruction == "You are a scorer."


def test_json_calls_are_deterministic_with_minimal_thinking(monkeypatch):
    p, models = _provider(monkeypatch, {"gemini-flash-lite-latest": ['{"ok": true}']})
    assert p.generate_json("x") == {"ok": True}
    cfg = models.calls[0]["config"]
    assert cfg.temperature == 0
    assert cfg.response_mime_type == "application/json"
    assert cfg.thinking_config.thinking_level.value.lower() == "minimal"


def test_busy_model_falls_back_to_the_next_one(monkeypatch):
    p, models = _provider(monkeypatch, {
        "gemini-flash-lite-latest": [_busy()],
        "gemini-flash-latest": ['{"ok": true}'],
    })
    assert p.generate_json("x") == {"ok": True}
    assert [c["model"] for c in models.calls] == ["gemini-flash-lite-latest", "gemini-flash-latest"]
    # flash rejects "minimal", so it gets its own lowest level
    assert models.calls[1]["config"].thinking_config.thinking_level.value.lower() == "low"


def test_quota_exhaustion_also_falls_back(monkeypatch):
    p, _ = _provider(monkeypatch, {
        "gemini-flash-lite-latest": [_quota()],
        "gemini-flash-latest": ["plain text"],
    })
    assert p.generate("x") == "plain text"


def test_every_model_down_raises_rate_limited(monkeypatch):
    p, _ = _provider(monkeypatch, {
        "gemini-flash-lite-latest": [_busy()],
        "gemini-flash-latest": [_quota()],
    })
    with pytest.raises(g.RateLimitedError):
        p.generate("x")


def test_empty_text_counts_as_unavailable(monkeypatch):
    p, _ = _provider(monkeypatch, {
        "gemini-flash-lite-latest": [""],
        "gemini-flash-latest": ["answer"],
    })
    assert p.generate("x") == "answer"


def test_rejected_thinking_setting_retries_without_it(monkeypatch):
    bad = errors.ClientError(400, {"error": {"message": "Thinking level MINIMAL is not supported"}})
    p, models = _provider(monkeypatch, {"gemini-flash-lite-latest": [bad, '{"ok": true}']})
    assert p.generate_json("x") == {"ok": True}
    assert models.calls[1]["config"].thinking_config is None


def test_a_real_client_error_is_not_swallowed(monkeypatch):
    bad_key = errors.ClientError(403, {"error": {"message": "API key not valid"}})
    p, _ = _provider(monkeypatch, {"gemini-flash-lite-latest": [bad_key]})
    with pytest.raises(errors.ClientError):
        p.generate("x")


def test_a_model_this_key_cannot_use_is_skipped(monkeypatch):
    gone = errors.ClientError(404, {"error": {"message": "no longer available to new users"}})
    p, models = _provider(monkeypatch, {
        "gemini-2.5-flash": [gone],
        "gemini-flash-lite-latest": ["answer"],
    })
    monkeypatch.setattr(g, "DEFAULT_PRO_MODEL", "gemini-2.5-flash")  # a stale Modal override
    assert p.generate("x") == "answer"


def test_a_timed_out_model_hands_off_to_the_next(monkeypatch):
    import httpx
    p, _ = _provider(monkeypatch, {
        "gemini-flash-lite-latest": [httpx.ReadTimeout("slow")],
        "gemini-flash-latest": ["answer"],
    })
    assert p.generate("x") == "answer"


def test_an_unrelated_400_is_not_retried_without_thinking(monkeypatch):
    bad = errors.ClientError(400, {"error": {"message": "Request payload size exceeds the limit"}})
    p, models = _provider(monkeypatch, {"gemini-flash-lite-latest": [bad, '{"ok": true}']})
    with pytest.raises(errors.ClientError):
        p.generate_json("x")
    assert len(models.calls) == 1
