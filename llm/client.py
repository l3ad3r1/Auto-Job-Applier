"""Minimal OpenAI-compatible chat client for local LLM servers (Ollama, LM
Studio, llama.cpp server). Stdlib-only — no extra dependencies."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


def chat(base_url: str, model: str, system: str, user: str,
         temperature: float = 0.0, timeout: int = 120) -> str:
    """Single-turn chat completion. Raises LLMError on any failure."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise LLMError(f"local LLM call failed: {e}") from e
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected LLM response shape: {data}") from e
