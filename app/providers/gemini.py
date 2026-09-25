"""Gemini provider, built for a free-tier key.

Per the gemini-api-conventions skill and Google's docs (checked 2026-09-25):
- One shared client. A client per call leaks its httpx pool and can die mid-request.
- The SDK's own retry (exponential backoff with jitter) on 408/429/5xx. SDK 2.0.0
  retries nothing unless retry_options is set.
- A model fallback chain. On a new free-tier key only the floating aliases work:
  gemini-flash-lite-latest (reliable) and gemini-flash-latest (stronger, often 503s
  under load). Rate limits are per project AND per model, so when one model is
  busy or out of quota the other usually isn't. RPD resets at midnight Pacific.
- The system prompt goes in system_instruction, not in the message text.
- Structured (JSON) calls run at temperature 0 with the lowest thinking level each
  model accepts: thinking tokens cost quota and time, and can leave the text empty.

Override with Modal Secret env vars: GEMINI_PRO_MODEL (scoring, drafting),
GEMINI_FLASH_MODEL (web-search calls), GEMINI_FALLBACK_MODELS (comma list).
`modal run app/admin.py::probe_model_quota` shows what the current key can use.
"""
import logging
import os
import threading
from typing import Optional

import httpx
from google import genai
from google.genai import errors, types

from app.providers import LLMProvider

log = logging.getLogger("app.providers.gemini")

DEFAULT_PRO_MODEL = os.environ.get("GEMINI_PRO_MODEL", "gemini-flash-lite-latest")
DEFAULT_FLASH_MODEL = os.environ.get("GEMINI_FLASH_MODEL", "gemini-flash-lite-latest")
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_FALLBACK_MODELS", "gemini-flash-lite-latest,gemini-flash-latest").split(",") if m.strip()]

# Lowest thinking level each model accepts (verified live 2026-09-25):
# flash-lite takes "minimal" and rejects thinking_budget=0; flash rejects "minimal".
_THINKING_LEVEL = {"gemini-flash-lite-latest": "minimal", "gemini-flash-latest": "low"}

# Per model: 1 call + 2 retries, 2s -> 4s (+ jitter), each call capped at 45s.
# Worst case ~2.5 min per model, ~5 min for the two-model chain, under the web
# container's 600s request limit. A stuck model should hand off, not hold on.
_RETRY = types.HttpRetryOptions(attempts=3, initial_delay=2.0, max_delay=30.0, jitter=1.0)
_TIMEOUT_MS = 45_000

_client: Optional[genai.Client] = None
_client_lock = threading.Lock()


def _get_client() -> genai.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = genai.Client(
                api_key=os.environ["GEMINI_API_KEY"],
                http_options=types.HttpOptions(timeout=_TIMEOUT_MS, retry_options=_RETRY),
            )
        return _client


class RateLimitedError(RuntimeError):
    """Every model in the chain stayed rate-limited, out of quota, or unavailable."""


def _is_rate_limit(err: Exception) -> bool:
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    if status in (429, 503):
        return True
    msg = str(err)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg


def _model_chain(target: str) -> list[str]:
    chain = [target]
    for m in FALLBACK_MODELS:
        if m not in chain:
            chain.append(m)
    return chain


class GeminiProvider(LLMProvider):
    def __init__(self):
        self.client = _get_client()

    def _config(self, model: str, system: Optional[str], json_mode: bool,
                web_search: bool, thinking: bool) -> Optional[types.GenerateContentConfig]:
        kw: dict = {}
        if system:
            kw["system_instruction"] = system
        if web_search:
            kw["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        # Gemini rejects response_mime_type=application/json alongside tools.
        if json_mode and not web_search:
            kw["response_mime_type"] = "application/json"
        if json_mode:
            kw["temperature"] = 0
            if thinking and model in _THINKING_LEVEL:
                kw["thinking_config"] = types.ThinkingConfig(thinking_level=_THINKING_LEVEL[model])
        return types.GenerateContentConfig(**kw) if kw else None

    def _call(self, model: str, prompt: str, system, json_mode, web_search) -> str:
        cfg = self._config(model, system, json_mode, web_search, thinking=True)
        try:
            resp = self.client.models.generate_content(model=model, contents=prompt, config=cfg)
        except errors.ClientError as e:
            # A model that changes which thinking levels it accepts shouldn't break
            # scoring: retry once without the thinking setting.
            if (getattr(e, "code", None) == 400 and cfg is not None and cfg.thinking_config
                    and "thinking" in str(e).lower()):
                cfg = self._config(model, system, json_mode, web_search, thinking=False)
                resp = self.client.models.generate_content(model=model, contents=prompt, config=cfg)
            else:
                raise
        text = resp.text
        if not text:
            raise errors.ServerError(503, {"error": {"message": "empty response text"}})
        return text

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> str:
        target = model_override or (DEFAULT_FLASH_MODEL if web_search else DEFAULT_PRO_MODEL)
        last_err: Optional[Exception] = None
        for model in _model_chain(target):
            try:
                return self._call(model, prompt, system, json_mode, web_search)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                # The SDK retried these already; a slow or unreachable model hands
                # off to the next one instead of failing the whole request.
                last_err = e
                log.warning("[gemini] %s timed out after retries; trying next model", model)
                continue
            except (errors.ClientError, errors.ServerError) as e:
                # 404: this key can't use that model (e.g. a stale GEMINI_PRO_MODEL
                # naming a model closed to new projects). Skip it like a busy one.
                if isinstance(e, errors.ClientError) and not (_is_rate_limit(e) or getattr(e, "code", None) == 404):
                    raise
                last_err = e
                log.warning("[gemini] %s unavailable after retries (%s); trying next model",
                            model, getattr(e, "code", type(e).__name__))
        raise RateLimitedError(
            f"Gemini unavailable on every model ({', '.join(_model_chain(target))}): {last_err}"
        ) from last_err

    def generate_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        web_search: bool = False,
        model_override: Optional[str] = None
    ) -> dict:
        """Generate and parse JSON using shared extraction logic."""
        from app.providers import extract_json
        raw = self.generate(
            prompt,
            system=system,
            json_mode=True,
            web_search=web_search,
            model_override=model_override
        )
        return extract_json(raw)

    def name(self) -> str:
        return f"gemini/{DEFAULT_PRO_MODEL}"
