import os
from typing import Optional

import anthropic

from app.providers import LLMProvider


class AnthropicProvider(LLMProvider):
    """Claude-based LLM provider as a fallback for Gemini."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> str:
        """Generate text using Claude.

        json_mode: if True, append instruction to request JSON-only output.
        web_search: not supported in fallback (logs warning, proceeds without search).
        """
        if web_search:
            print("[anthropic] web_search not supported in fallback — answering from model knowledge")

        target_model = model_override or self.model

        # Build user content
        user_content = prompt
        if json_mode:
            user_content = f"{prompt}\n\nRespond with ONLY valid JSON. No prose, no markdown fences."

        # Call Anthropic API. Only pass `system` when set — the SDK omits it by
        # default (NOT_GIVEN sentinel); an explicit None can serialize to a null
        # system field and 400.
        create_kwargs: dict = {
            "model": target_model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": user_content}],
        }
        if system:
            create_kwargs["system"] = system
        response = self.client.messages.create(**create_kwargs)

        # Extract text from response
        text_blocks = [block.text for block in response.content if block.type == "text"]
        if not text_blocks:
            return ""
        return "".join(text_blocks)

    def generate_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        web_search: bool = False,
        model_override: Optional[str] = None,
    ) -> dict:
        """Generate and parse JSON using shared extraction logic."""
        from app.providers import extract_json

        raw = self.generate(
            prompt,
            system=system,
            json_mode=True,
            web_search=web_search,
            model_override=model_override,
        )
        return extract_json(raw)

    def name(self) -> str:
        return f"anthropic/{self.model}"
