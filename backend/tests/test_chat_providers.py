import requests
import pytest

from chat import opencode


class _FakeRequests:
    """Stub replacing `chat.opencode.requests`: either raises or returns a fixed response."""

    RequestException = requests.RequestException

    def __init__(self, response=None, raises=False):
        self._response = response
        self._raises = raises

    def post(self, *args, **kwargs):
        if self._raises:
            raise requests.RequestException("down")
        return self._response


def _resp(status_code, payload=None):
    return type("Resp", (), {"status_code": status_code,
                             "json": lambda self: payload if payload is not None else {}})()


def test_raises_not_configured_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    with pytest.raises(opencode.ChatError) as e:
        opencode.complete([{"role": "user", "content": "hi"}], [], "m")
    assert e.value.code == "not_configured"


def test_network_failure_is_unavailable(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "k")
    monkeypatch.setattr(opencode, "requests", _FakeRequests(raises=True))
    with pytest.raises(opencode.ChatError) as e:
        opencode.complete([{"role": "user", "content": "hi"}], [], "m")
    assert e.value.code == "unavailable"


def test_bad_status_is_provider_error(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "k")
    monkeypatch.setattr(opencode, "requests", _FakeRequests(_resp(500)))
    with pytest.raises(opencode.ChatError) as e:
        opencode.complete([{"role": "user", "content": "hi"}], [], "m")
    assert e.value.code == "provider_error"
