"""
Provider-switch test: verifies that LLM_PROVIDER routes to the correct provider
class without making real API calls.

All three providers accept a fake API key at init time — they only validate keys
when an actual generate() call is made.
"""
import pytest


@pytest.fixture(autouse=True)
def _dummy_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def test_gemini_default(_dummy_keys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    from app.providers import get_provider, FallbackProvider
    p = get_provider()
    assert isinstance(p, FallbackProvider), f"expected FallbackProvider, got {type(p).__name__}"


def test_anthropic_provider(_dummy_keys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    from app.providers import get_provider
    from app.providers.anthropic_provider import AnthropicProvider
    p = get_provider()
    assert isinstance(p, AnthropicProvider), f"expected AnthropicProvider, got {type(p).__name__}"


def test_openai_provider(_dummy_keys, monkeypatch):
    pytest.importorskip("openai", reason="openai package not installed")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    from app.providers import get_provider
    from app.providers.openai_provider import OpenAIProvider
    p = get_provider()
    assert isinstance(p, OpenAIProvider), f"expected OpenAIProvider, got {type(p).__name__}"


def test_unknown_provider_falls_back_to_gemini(_dummy_keys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-provider")
    from app.providers import get_provider, FallbackProvider
    p = get_provider()
    assert isinstance(p, FallbackProvider), (
        f"unknown provider should fall back to Gemini/FallbackProvider, got {type(p).__name__}"
    )


def test_fallback_reraises_when_anthropic_key_is_placeholder(_dummy_keys, monkeypatch):
    """A RateLimitedError must not surface as a confusing Anthropic 401 when the
    fallback key is still the deploy-unblocking placeholder — it should re-raise
    the original error instead of attempting the doomed Anthropic call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-yet-configured")
    from app.providers import FallbackProvider
    from app.providers.gemini import RateLimitedError

    p = FallbackProvider()
    p._primary = type("_P", (), {"generate": lambda self, *a, **kw: (_ for _ in ()).throw(RateLimitedError("429"))})()

    with pytest.raises(RateLimitedError):
        p.generate("prompt")
    assert p._fallback is None, "should not have attempted to construct AnthropicProvider"


def test_fallback_uses_anthropic_when_key_is_real(_dummy_keys, monkeypatch):
    from app.providers import FallbackProvider
    from app.providers.gemini import RateLimitedError

    p = FallbackProvider()
    p._primary = type("_P", (), {"generate": lambda self, *a, **kw: (_ for _ in ()).throw(RateLimitedError("429"))})()
    p._fallback = type("_F", (), {"generate": lambda self, *a, **kw: "claude-response"})()

    assert p.generate("prompt") == "claude-response"
