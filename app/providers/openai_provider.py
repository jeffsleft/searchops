"""
OpenAI LLM provider for SearchOps.

Configure via env vars:
  OPENAI_API_KEY   — required (BYO key from platform.openai.com)
  OPENAI_MODEL     — default: gpt-4o-mini (override to gpt-4o for higher quality)
  OPENAI_BASE_URL  — optional (override for compatible APIs, e.g. LM Studio)

web_search is not supported — OpenAI's web browsing tools are not wired here.
Calls that request web_search proceed without search and log a warning.
"""
import os
from typing import Optional

from app.providers import LLMProvider, extract_json


class OpenAIProvider(LLMProvider):
    """OpenAI ChatCompletion provider (gpt-4o / gpt-4o-mini / compatible)."""

    def __init__(self):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai package not installed. Add 'openai' to requirements.txt."
            )

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY env var not set. "
                "Get a key at platform.openai.com and add it to your Modal Secret."
            )

        base_url = os.environ.get("OPENAI_BASE_URL")  # None → uses default OpenAI endpoint
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> str:
        """Generate text using OpenAI ChatCompletion.

        json_mode: if True, sets response_format={"type":"json_object"} and
                   appends a JSON-only instruction. Requires at least one
                   "json" mention in the prompt (OpenAI requirement).
        web_search: not supported — logs a warning and proceeds without search.
        """
        if web_search:
            print("[openai] web_search not supported — answering from model knowledge")

        target_model = model_override or self.model

        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        user_content = prompt
        if json_mode:
            user_content = f"{prompt}\n\nRespond with ONLY valid JSON. No prose, no markdown fences."

        messages.append({"role": "user", "content": user_content})

        kwargs: dict = {
            "model": target_model,
            "messages": messages,
            "max_tokens": 4096,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        return content

    def generate_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> dict:
        """Generate and parse JSON using shared extraction logic."""
        raw = self.generate(
            prompt,
            system=system,
            json_mode=True,
            web_search=web_search,
            model_override=model_override,
        )
        return extract_json(raw)

    def name(self) -> str:
        return f"openai/{self.model}"
