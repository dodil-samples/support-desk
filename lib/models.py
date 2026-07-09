"""Ignite Models client — OpenAI-compatible chat + embeddings.

Ignite serves `POST /v1/chat/completions` and `/v1/embeddings` in the OpenAI shape,
often wrapped in a `{"data": <openai response>, "status": "success"}` envelope. Auth
is the same service-account bearer used for K3.

Env:
  MODEL_API_BASE   default https://api.dev.dodil.io/v1
  MODEL_NAME       default moonshot-v1-auto (fast instruct — triage in ~2s; set a
                   reasoning model like kimi-k2.6 only if you can afford ~30s/call)
  EMBED_MODEL      default jina-embeddings-v4
"""

from __future__ import annotations

import json
import os

from . import auth, http

BASE = os.getenv("MODEL_API_BASE", "https://api.dev.dodil.io/v1").rstrip("/")
CHAT_MODEL = os.getenv("MODEL_NAME", "moonshot-v1-auto")
EMBED_MODEL = os.getenv("EMBED_MODEL", "jina-embeddings-v4")


def _bearer() -> str:
    return os.getenv("MODEL_API_KEY") or auth.get_token()


def _unwrap(data: object) -> object:
    if isinstance(data, dict) and "choices" not in data and "data" in data:
        if data.get("status") == "error":
            raise RuntimeError(f"model error: {data.get('data')}")
        return data["data"]
    return data


def chat(messages: list[dict], max_tokens: int = 2500) -> str:
    """One chat completion; returns the assistant's text.

    NB: reasoning models (e.g. kimi-k2.6) spend tokens on hidden `reasoningContent`
    BEFORE emitting `content`, so the budget must cover reasoning + answer or
    `content` comes back empty (finish_reason=length). Keep this generous.
    """
    payload = {"model": CHAT_MODEL, "messages": messages, "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {_bearer()}", "Content-Type": "application/json"}
    status, data = http.request_json(
        "POST", f"{BASE}/chat/completions", headers=headers, json_body=payload, timeout=90
    )
    if status >= 300:
        raise RuntimeError(f"chat HTTP {status}: {str(data)[:200]}")
    data = _unwrap(data)
    try:
        msg = data["choices"][0]["message"]
        return (msg.get("content") or msg.get("reasoningContent") or "").strip()
    except (KeyError, IndexError, TypeError):
        return (data.get("content") if isinstance(data, dict) else str(data)) or ""


def chat_json(system: str, user: str, max_tokens: int = 2000) -> dict:
    """Chat that must return strict JSON; parses it, tolerating code fences.

    Budget covers a reasoning model's think-then-answer (600 truncated kimi's
    reasoning and left an empty answer)."""
    txt = chat([{"role": "system", "content": system},
                {"role": "user", "content": user}], max_tokens=max_tokens)
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s.strip("`")
        s = s[4:] if s.lower().startswith("json") else s
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(s[a : b + 1])
        except ValueError:
            pass
    return {}


def embed(text: str) -> list[float]:
    """Return an embedding vector (empty list if the embedder is unavailable)."""
    headers = {"Authorization": f"Bearer {_bearer()}", "Content-Type": "application/json"}
    status, data = http.request_json(
        "POST", f"{BASE}/embeddings", headers=headers,
        json_body={"model": EMBED_MODEL, "input": text}, timeout=60,
    )
    if status >= 300:
        return []
    data = _unwrap(data)
    try:
        return data["data"][0]["embedding"] or []
    except (KeyError, IndexError, TypeError):
        return []
