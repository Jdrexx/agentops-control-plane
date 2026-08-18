import email.message
import io
import json
import urllib.error

import pytest

from src.agentops.providers import ProviderError, ProviderRegistry


class Response:
    def __init__(self, body):
        self.body = body.encode() if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.body

    def __iter__(self):
        return iter([self.body])


def _http_error(code: int, retry_after: int | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://provider.test", code, "error", headers, io.BytesIO(b"{}"))


def _monkeypatch_urlopen(monkeypatch, results):
    calls = {"count": 0}

    def fake_urlopen(request, timeout=60):
        calls["count"] += 1
        result = results[min(calls["count"] - 1, len(results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def test_openai_usage_is_parsed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    body = json.dumps(
        {
            "choices": [{"message": {"content": "hello world"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
    )
    _monkeypatch_urlopen(monkeypatch, [Response(body)])
    result = ProviderRegistry().generate_detailed("openai", "gpt-test", "hello")
    assert result.text == "hello world"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.finish_reason == "stop"
    assert result.latency_ms is not None


def test_anthropic_usage_is_parsed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    body = json.dumps(
        {
            "content": [{"text": "claude says hi"}],
            "usage": {"input_tokens": 9, "output_tokens": 4},
            "stop_reason": "end_turn",
        }
    )
    _monkeypatch_urlopen(monkeypatch, [Response(body)])
    result = ProviderRegistry().generate_detailed("anthropic", "claude-test", "hi")
    assert result.text == "claude says hi"
    assert result.input_tokens == 9
    assert result.output_tokens == 4
    assert result.finish_reason == "end_turn"


def test_ollama_eval_counts_are_parsed(monkeypatch):
    body = json.dumps(
        {"response": "local result", "prompt_eval_count": 6, "eval_count": 3}
    )
    _monkeypatch_urlopen(monkeypatch, [Response(body)])
    result = ProviderRegistry().generate_detailed("ollama", "llama-test", "hello")
    assert result.text == "local result"
    assert result.input_tokens == 6
    assert result.output_tokens == 3


def test_mock_reports_exact_counts():
    result = ProviderRegistry().generate_detailed("mock", "mock-small", "two word prompt")
    assert result.text
    assert result.input_tokens == 3  # "two word prompt"
    assert result.output_tokens == len(result.text.split())
    assert result.finish_reason == "stop"


def test_retry_on_429_honors_retry_after(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    body = json.dumps(
        {
            "choices": [{"message": {"content": "recovered"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    calls = _monkeypatch_urlopen(
        monkeypatch, [_http_error(429, retry_after=0), Response(body)]
    )
    result = ProviderRegistry().generate("openai", "gpt-test", "hello")
    assert result == "recovered"
    assert calls["count"] == 2


def test_retry_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    body = json.dumps(
        {"content": [{"text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    )
    calls = _monkeypatch_urlopen(
        monkeypatch, [_http_error(502), _http_error(503), Response(body)]
    )
    result = ProviderRegistry().generate("anthropic", "claude-test", "hi")
    assert result == "ok"
    assert calls["count"] == 3


def test_auth_errors_do_not_retry(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = _monkeypatch_urlopen(monkeypatch, [_http_error(401)])
    with pytest.raises(ProviderError):
        ProviderRegistry().generate("openai", "gpt-test", "hello")
    assert calls["count"] == 1


def test_retries_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AGENTOPS_PROVIDER_RETRIES", "0")
    calls = _monkeypatch_urlopen(monkeypatch, [_http_error(429, retry_after=0)])
    with pytest.raises(ProviderError):
        ProviderRegistry().generate("openai", "gpt-test", "hello")
    assert calls["count"] == 1


def test_stream_retries_and_clears_partial_chunks(monkeypatch):
    body = b'{"response":"final "}\n{"response":"result","done":true}\n'

    class StreamResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def __iter__(self):
            return iter(self.payload.split(b"\n"))

    calls = {"count": 0}

    def fake_urlopen(request, timeout=60):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(429, retry_after=0)
        return StreamResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    chunks: list[str] = []
    result = ProviderRegistry().generate("ollama", "llama-test", "hello", on_chunk=chunks.append)
    assert result == "final result"
    assert "".join(chunks) == "final result"
    assert calls["count"] == 2
