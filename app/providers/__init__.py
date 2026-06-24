import json
import re
from abc import ABC, abstractmethod
from typing import Optional


def extract_json(raw: str) -> dict:
    """Extract and parse JSON from raw text with multiple fallback strategies.

    Attempts:
    1. Direct json.loads
    2. Strip markdown ```json fences
    3. Extract first {...} block from mixed prose+JSON

    Returns {} if all attempts fail.
    """
    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Extract first {...} block from mixed prose+JSON response (common with web search)
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    print(f"extract_json: all parse attempts failed. Response snippet: {raw[:300]}")
    return {}


class LLMProvider(ABC):
    @abstractmethod
    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> str:
        pass

    @abstractmethod
    def name(self) -> str:
        pass


def get_provider() -> "LLMProvider":
    """Return the configured LLM provider.

    Reads LLM_PROVIDER env var (default: "gemini"):
      gemini    — GeminiProvider (primary) wrapped in FallbackProvider with
                  automatic Anthropic fallback on RateLimitedError (existing behaviour)
      anthropic — AnthropicProvider as primary (no fallback)
      openai    — OpenAIProvider as primary (no fallback)

    To change provider, set LLM_PROVIDER in your Modal Secret (recruiting-secrets)
    or local .env. BYO-key: set the matching API key env var for your chosen provider.
    """
    import os
    provider_name = os.environ.get("LLM_PROVIDER", "gemini").lower().strip()

    if provider_name == "anthropic":
        from app.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider()

    if provider_name == "openai":
        from app.providers.openai_provider import OpenAIProvider
        return OpenAIProvider()

    # Default: gemini with automatic Anthropic fallback on RateLimitedError
    return FallbackProvider()


class FallbackProvider(LLMProvider):
    """Wraps GeminiProvider (primary) with automatic Anthropic fallback on RateLimitedError."""

    def __init__(self):
        self._primary = None
        self._fallback = None
        self._init_primary()

    def _init_primary(self):
        """Eagerly initialize GeminiProvider."""
        from app.providers.gemini import GeminiProvider
        self._primary = GeminiProvider()

    def _get_fallback(self):
        """Lazily initialize AnthropicProvider on first fallback."""
        if self._fallback is None:
            from app.providers.anthropic_provider import AnthropicProvider
            self._fallback = AnthropicProvider()
        return self._fallback

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> str:
        """Try Gemini first; on RateLimitedError, fall back to Anthropic."""
        from app.providers.gemini import RateLimitedError

        try:
            return self._primary.generate(
                prompt,
                system=system,
                json_mode=json_mode,
                web_search=web_search,
                model_override=model_override,
            )
        except RateLimitedError as e:
            print(f"[fallback] Gemini rate-limited: {e}. Retrying with Anthropic.")
            fallback = self._get_fallback()
            return fallback.generate(
                prompt,
                system=system,
                json_mode=json_mode,
                web_search=web_search,
                model_override=model_override,
            )

    def generate_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> dict:
        """Try Gemini first; on RateLimitedError, fall back to Anthropic."""
        from app.providers.gemini import RateLimitedError

        try:
            return self._primary.generate_json(
                prompt,
                system=system,
                web_search=web_search,
                model_override=model_override,
            )
        except RateLimitedError as e:
            print(f"[fallback] Gemini rate-limited: {e}. Retrying with Anthropic.")
            fallback = self._get_fallback()
            return fallback.generate_json(
                prompt,
                system=system,
                web_search=web_search,
                model_override=model_override,
            )

    def name(self) -> str:
        return f"fallback({self._primary.name()})"
