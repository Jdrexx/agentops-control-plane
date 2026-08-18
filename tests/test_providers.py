import json

import pytest

from src.agentops.providers import ProviderError, ProviderRegistry, _mock_fingerprint


def test_mock_provider_is_deterministic_and_offline():
    registry = ProviderRegistry()
    prompt = "Please summarize the invoice issue"
    first = registry.generate("mock", "mock-small", prompt)
    second = registry.generate("mock", "mock-small", prompt)
    assert first == second
    assert "[mock:" in first
    assert len(first) > 20


def test_mock_provider_defaults_model_and_accepts_unknown_models():
    registry = ProviderRegistry()
    assert registry.generate("mock", "", "hello")
    assert registry.generate("mock", "any-custom-model", "hello")


def test_mock_provider_streams_chunks_that_reconstruct_output(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_MOCK_CHUNK_MS", "0")
    chunks: list[str] = []
    result = ProviderRegistry().generate(
        "mock", "mock-small", "two word prompt", on_chunk=chunks.append
    )
    assert chunks
    assert "".join(chunks) == result


def test_mock_provider_uses_pinned_script(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AGENTOPS_MOCK_SCRIPT_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTOPS_MOCK_CHUNK_MS", "0")
    fingerprint = _mock_fingerprint("mock-small", "", "pinned prompt")
    (tmp_path / f"{fingerprint[:16]}.txt").write_text("PINNED RESPONSE")
    assert ProviderRegistry().generate("mock", "mock-small", "pinned prompt") == "PINNED RESPONSE"


def test_mock_provider_different_prompts_differ():
    registry = ProviderRegistry()
    assert registry.generate("mock", "mock-small", "question one") != registry.generate(
        "mock", "mock-small", "question two"
    )


def test_mock_provider_reported_configured_and_local():
    statuses = {item["name"]: item for item in ProviderRegistry().status()}
    assert statuses["mock"]["configured"] is True
    assert statuses["mock"]["local"] is True
    assert statuses["mock"]["default_model"] == "mock-small"


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
