"""Google Gemini over HTTPS: a single, tool-less text completion.

Same boundary as ragra/adapters/ai.py (the Hermes one-shot): the model is
handed a prompt and returns text, nothing else. It has no tools and no
database access, so it cannot change a task, a deadline or a reminder - it can
only describe the read-only snapshot it was given. Unlike Hermes it is plain
HTTPS, so it works on a hosted deployment.
"""

from __future__ import annotations

import requests

from ragra.adapters.ai import AIAdapterError

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def ask_gemini(api_key: str | None, model: str, prompt: str, *, timeout_seconds: int = 25) -> str:
    if not api_key:
        raise AIAdapterError("AI is not configured (RAGRA_GEMINI_API_KEY not set)")
    try:
        response = requests.post(
            _ENDPOINT.format(model=model),
            headers={"x-goog-api-key": api_key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700},
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise AIAdapterError(str(exc).replace(api_key, "***")) from exc
    if response.status_code >= 300:
        raise AIAdapterError(
            f"Gemini HTTP {response.status_code}: {response.text[:200]}".replace(api_key, "***")
        )
    try:
        parts = response.json()["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts).strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIAdapterError("Gemini returned no answer") from exc
    if not text:
        raise AIAdapterError("Gemini returned an empty answer")
    return text
