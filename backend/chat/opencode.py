"""Minimal OpenCode chat-completions client (tool calling). The API key never leaves the server."""
import os

import requests

from .errors import ChatError

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/api/v1")


def complete(messages: list[dict], tools: list[dict], model: str, *, timeout: float = 90,
             key: str = "") -> dict:
    """Returns the assistant message: {"content": str, "tool_calls": [{"id","name","arguments"}]}.

    `key` is a per-request override from the caller (browser-supplied); when empty the
    server env var is used. It is never logged or returned.
    """
    key = key or os.environ.get("OPENCODE_API_KEY", "")
    if not key:
        raise ChatError("not_configured", "the chat is not configured: OPENCODE_API_KEY is missing")
    body = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions", json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {key}", "X-Title": "Singularity"},
        )
    except requests.RequestException as exc:
        raise ChatError("unavailable", "OpenCode could not be reached; try again shortly") from exc
    if resp.status_code != 200:
        raise ChatError("provider_error", f"OpenCode returned HTTP {resp.status_code}")
    try:
        msg = resp.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ChatError("provider_error", "OpenCode sent an unreadable reply") from exc
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        calls.append({"id": c.get("id") or f"call_{len(calls)}", "name": fn.get("name", ""),
                      "arguments": fn.get("arguments") or "{}"})
    return {"content": msg.get("content") or "", "tool_calls": calls}
