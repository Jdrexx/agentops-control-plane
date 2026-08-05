import json

import pytest

from src.agentops.providers import ProviderError, ProviderRegistry


def test_provider_requires_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        ProviderRegistry().generate("openai", "model", "hello")


def test_ollama_response_is_normalized(monkeypatch: pytest.MonkeyPatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return json.dumps({"response": "local result"}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    assert ProviderRegistry().generate("ollama", "llama", "hello") == "local result"


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderError, match="unknown provider"):
        ProviderRegistry().generate("missing", "model", "hello")


def test_ollama_stream_forwards_chunks(monkeypatch: pytest.MonkeyPatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def __iter__(self):
            return iter(
                [
                    b'{"response":"local "}\n',
                    b'{"response":"result","done":true}\n',
                ]
            )

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    chunks: list[str] = []
    result = ProviderRegistry().generate("ollama", "llama", "hello", on_chunk=chunks.append)
    assert result == "local result"
    assert chunks == ["local ", "result"]
