"""
evals/providers.py — uniform text completion across providers, so the harness isn't
tied to one vendor. Choose per case with "provider": "anthropic" | "gemini" | "local",
or set a default with the EVALS_PROVIDER env var (falls back to "gemini").

Credentials:
  anthropic → ANTHROPIC_API_KEY
  gemini    → GEMINI_API_KEY (or GOOGLE_API_KEY); pip install google-genai
  local     → none (LM Studio at OPENAI_BASE_URL, default http://localhost:1234/v1)
"""

from __future__ import annotations

import json
import os
import re

import httpx

DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "gemini": "gemini-2.5-pro",       # see the gemini-api-conventions skill for model strategy
    "local": "local-model",            # whatever LM Studio has loaded
}

LOCAL_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:1234/v1")


def complete(*, provider=None, model=None, system, user, max_tokens=2048, thinking=False) -> str:
    """One text completion, dispatched to the chosen provider. Returns plain text."""
    provider = provider or os.getenv("EVALS_PROVIDER", "gemini")
    model = model or DEFAULT_MODELS[provider]
    if provider == "anthropic":
        return _anthropic(model, system, user, max_tokens, thinking)
    if provider == "gemini":
        return _gemini(model, system, user, max_tokens)
    if provider == "local":
        return _local(model, system, user, max_tokens)
    raise ValueError(f"unknown provider: {provider!r} (use anthropic | gemini | local)")


def _anthropic(model, system, user, max_tokens, thinking) -> str:
    import anthropic

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY
    kw = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if thinking:
        kw["thinking"] = {"type": "adaptive"}
    resp = client.messages.create(**kw)
    return "".join(b.text for b in resp.content if b.type == "text")


def _gemini(model, system, user, max_tokens) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client()  # GEMINI_API_KEY or GOOGLE_API_KEY
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        ),
    )
    return resp.text or ""


def _local(model, system, user, max_tokens) -> str:
    # OpenAI-compatible endpoint (LM Studio). No real key needed.
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    r = httpx.post(
        f"{LOCAL_BASE_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', 'local')}"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """Tolerant JSON parse for judge output: strip code fences, else grab the first {...}."""
    t = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))
