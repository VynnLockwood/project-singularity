"""Pick the chat client for the configured provider."""
from . import opencode, openrouter
from .errors import ChatError

# Resolve the client module's function at call time, so patching openrouter.complete / opencode.complete still works.
_CLIENTS = {
    "openrouter": lambda *a, **kw: openrouter.complete(*a, **kw),
    "opencode": lambda *a, **kw: opencode.complete(*a, **kw),
}


def complete(provider: str, messages: list[dict], tools: list[dict], model: str,
             key: str = "") -> dict:
    """Call the configured provider's client.

    `key` is an optional per-request override (browser-supplied, never persisted or
    logged); empty means fall back to the server env var, preserving today's behaviour.
    """
    client = _CLIENTS.get(provider)
    if client is None:
        raise ChatError("not_configured", f"unknown chat provider {provider!r}")
    return client(messages, tools, model, key=key)
