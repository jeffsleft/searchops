import os
import time
from typing import Optional

from google import genai
from google.genai import errors, types

from app.providers import LLMProvider

# Pinned to gemini-2.5-flash because the API key has limit:0 on pro tier and 2.0
# series (verified via probe_model_quota 2026-05-15). When billing is enabled,
# override via Modal Secret env vars GEMINI_PRO_MODEL / GEMINI_FLASH_MODEL.
DEFAULT_PRO_MODEL = os.environ.get("GEMINI_PRO_MODEL", "gemini-2.5-flash")
DEFAULT_FLASH_MODEL = os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.5-flash")

_BACKOFF_SCHEDULE = (15, 30, 60, 120, 240)


class RateLimitedError(RuntimeError):
    """Raised after all backoff attempts are exhausted on a 429."""


def _is_rate_limit(err: Exception) -> bool:
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    if status in (429, 503):
        return True
    msg = str(err)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg


class GeminiProvider(LLMProvider):
    def __init__(self):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> str:
        contents = []
        if system:
            contents.append(system)
        contents.append(prompt)

        config_kwargs: dict = {}

        if web_search:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

        # Gemini doesn't allow response_mime_type=application/json with web_search tools
        if json_mode and not web_search:
            config_kwargs["response_mime_type"] = "application/json"

        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        # Use override if provided, else Pro for most things, Flash if web_search is on
        target_model = model_override or (DEFAULT_FLASH_MODEL if web_search else DEFAULT_PRO_MODEL)

        last_err: Optional[Exception] = None
        for attempt, wait in enumerate((0,) + _BACKOFF_SCHEDULE):
            if wait:
                print(f"[gemini] 429 backoff: sleeping {wait}s before retry {attempt}/{len(_BACKOFF_SCHEDULE)}")
                time.sleep(wait)
            try:
                response = self.client.models.generate_content(
                    model=target_model,
                    contents=contents,
                    config=config,
                )
                return response.text
            except errors.ClientError as e:
                last_err = e
                if not _is_rate_limit(e):
                    raise
            except Exception as e:
                if _is_rate_limit(e):
                    last_err = e
                    continue
                raise

        raise RateLimitedError(
            f"Gemini RESOURCE_EXHAUSTED after {len(_BACKOFF_SCHEDULE)} retries on model {target_model}: {last_err}"
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
