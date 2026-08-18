from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    endpoint: str
    api_key_env: str | None
    default_model: str


@dataclass(frozen=True)
class ProviderResult:
    """A provider response with usage metadata.

    ``input_tokens``/``output_tokens`` are provider-reported where available
    (Ollama eval counts, OpenAI/Anthropic usage objects, exact mock counts)
    and ``None`` for streamed responses; callers fall back to estimates.
    """

    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    latency_ms: float | None = None


def _backoff_delay(error: Any, attempt: int) -> float:
    """Backoff for provider retries: honor Retry-After, else exponential capped."""
    retry_after: float | None = None
    headers = getattr(error, "headers", None)
    if headers is not None:
        value = headers.get("Retry-After")
        if value:
            try:
                retry_after = max(0, min(int(value), 30))
            except ValueError:
                retry_after = None
    if retry_after is not None:
        return retry_after
    return float(min(2**attempt, 8))


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _retryable(error: Exception) -> bool:
    """408/429/5xx and transport errors retry; auth and invalid requests never do."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429) or 500 <= error.code < 600
    return isinstance(error, (urllib.error.URLError, TimeoutError))


PROVIDERS = {
    "mock": ProviderConfig("mock", "", None, "mock-small"),
    "ollama": ProviderConfig("ollama", "http://127.0.0.1:11434", None, "llama3.2"),
    "openai": ProviderConfig("openai", "https://api.openai.com", "OPENAI_API_KEY", "gpt-4o-mini"),
    "anthropic": ProviderConfig(
        "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-3-5-haiku-latest"
    ),
}


def _mock_fingerprint(model: str, system: str, prompt: str) -> str:
    """Stable key for (model, system, prompt): same inputs -> same answer, forever."""
    return hashlib.sha256("\x1f".join((model, system, prompt)).encode("utf-8")).hexdigest()


def _mock_answer(model: str, system: str, prompt: str, script_dir: Path) -> str:
    """Deterministic offline response.

    A pinned script at ``<script_dir>/<fingerprint[:16]>.txt`` wins when present,
    so demos and tests can guarantee exact output (e.g. an evaluation that fails
    on workflow v1 and passes on v2). A ``<fingerprint[:16]>.fail`` file raises a
    deterministic ``ProviderError`` with the file contents as the message —
    offline failure injection for incident demos and retry-path tests. Unpinned
    prompts fall back to stable synthetic filler derived from the fingerprint.
    """
    fingerprint = _mock_fingerprint(model, system, prompt)
    fail_script = script_dir / f"{fingerprint[:16]}.fail"
    if fail_script.is_file():
        raise ProviderError(
            fail_script.read_text(encoding="utf-8").strip() or "simulated provider outage"
        )
    pinned = script_dir / f"{fingerprint[:16]}.txt"
    if pinned.is_file():
        return pinned.read_text(encoding="utf-8").strip()
    seed = int(fingerprint[:8], 16)
    tone = ("Acknowledged", "Understood", "Confirmed", "Noted")[seed % 4]
    subject = " ".join(prompt.split()[:8]) or "the request"
    return (
        f"{tone}. Regarding {subject} -- I have reviewed the details and "
        f"recommend proceeding with the documented resolution path. "
        f"[mock:{fingerprint[:8]}]"
    )


class ProviderRegistry:
    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": config.name,
                "default_model": config.default_model,
                "configured": config.api_key_env is None or bool(os.getenv(config.api_key_env)),
                "local": config.name in {"ollama", "mock"},
            }
            for config in PROVIDERS.values()
        ]

    def generate(
        self,
        provider: str,
        model: str,
        prompt: str,
        system: str = "",
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        return self.generate_detailed(provider, model, prompt, system, on_chunk).text

    def generate_detailed(
        self,
        provider: str,
        model: str,
        prompt: str,
        system: str = "",
        on_chunk: Callable[[str], None] | None = None,
    ) -> ProviderResult:
        """Generate text and return the result with usage metadata.

        Non-streaming calls carry provider-reported token usage; streaming
        calls report ``None`` usage and callers fall back to estimates.
        Retries: 408/429/5xx and transport errors retry up to
        ``AGENTOPS_PROVIDER_RETRIES`` (default 2) times with backoff,
        honoring ``Retry-After``. Authentication and invalid-request errors
        never retry.
        """
        if provider not in PROVIDERS:
            raise ProviderError(f"unknown provider: {provider}")
        config = PROVIDERS[provider]
        model = model or config.default_model
        started = time.perf_counter()
        if provider == "mock":
            return self._generate_mock(model, prompt, system, on_chunk)
        if provider == "ollama":
            if on_chunk:
                text = self._stream_request(
                    f"{os.getenv('OLLAMA_HOST', config.endpoint).rstrip('/')}/api/generate",
                    {"model": model, "prompt": prompt, "system": system, "stream": True},
                    {},
                    lambda event: str(event.get("response", "")),
                    on_chunk,
                )
                return ProviderResult(text, provider, model, latency_ms=_elapsed(started))
            response = self._request(
                f"{os.getenv('OLLAMA_HOST', config.endpoint).rstrip('/')}/api/generate",
                {"model": model, "prompt": prompt, "system": system, "stream": False},
                {},
            )
            return ProviderResult(
                str(response.get("response", "")),
                provider,
                model,
                input_tokens=response.get("prompt_eval_count"),
                output_tokens=response.get("eval_count"),
                finish_reason="stop",
                latency_ms=_elapsed(started),
            )
        api_key = os.getenv(config.api_key_env or "")
        if not api_key:
            raise ProviderError(f"{config.api_key_env} is not configured")
        if provider == "openai":
            messages = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": prompt}
            ]
            payload = {"model": model, "messages": messages}
            headers = {"Authorization": f"Bearer {api_key}"}
            if on_chunk:
                text = self._stream_request(
                    f"{config.endpoint}/v1/chat/completions",
                    {**payload, "stream": True},
                    headers,
                    lambda event: str(
                        event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    ),
                    on_chunk,
                    server_sent_events=True,
                )
                return ProviderResult(text, provider, model, latency_ms=_elapsed(started))
            response = self._request(
                f"{config.endpoint}/v1/chat/completions",
                payload,
                headers,
            )
            usage = response.get("usage") or {}
            choice = response["choices"][0]
            return ProviderResult(
                str(choice["message"]["content"]),
                provider,
                model,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                finish_reason=choice.get("finish_reason"),
                latency_ms=_elapsed(started),
            )
        if on_chunk:
            text = self._stream_request(
                f"{config.endpoint}/v1/messages",
                {
                    "model": model,
                    "max_tokens": 1024,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": True,
                },
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                lambda event: str(event.get("delta", {}).get("text", "")),
                on_chunk,
                server_sent_events=True,
            )
            return ProviderResult(text, provider, model, latency_ms=_elapsed(started))
        response = self._request(
            f"{config.endpoint}/v1/messages",
            {
                "model": model,
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        usage = response.get("usage") or {}
        return ProviderResult(
            str(response["content"][0]["text"]),
            provider,
            model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            finish_reason=response.get("stop_reason"),
            latency_ms=_elapsed(started),
        )

    def _generate_mock(
        self,
        model: str,
        prompt: str,
        system: str,
        on_chunk: Callable[[str], None] | None,
    ) -> ProviderResult:
        """Deterministic offline generation with optional paced streaming.

        Environment controls (read per call so tests can override them):
        - ``AGENTOPS_MOCK_CHUNK_MS`` — delay between streamed chunks (default 35;
          set to 0 in CI so tests are instant).
        - ``AGENTOPS_MOCK_LATENCY_MS`` — fixed latency before answering.
        - ``AGENTOPS_MOCK_SCRIPT_DIR`` — directory of pinned ``<fp16>.txt``
          response scripts (default ``examples/mock_responses``).

        Streamed chunks reconstruct the returned text exactly
        (``"".join(chunks) == text``), and token counts are exact (not
        estimates) for both modes.
        """
        chunk_ms = int(os.getenv("AGENTOPS_MOCK_CHUNK_MS", "35"))
        latency_ms = int(os.getenv("AGENTOPS_MOCK_LATENCY_MS", "0"))
        script_dir = Path(os.getenv("AGENTOPS_MOCK_SCRIPT_DIR", "examples/mock_responses"))
        if latency_ms:
            time.sleep(latency_ms / 1000)
        text = _mock_answer(model, system or "", prompt, script_dir)
        input_tokens = len((f"{system} {prompt}").split())
        output_tokens = len(text.split())
        if on_chunk is None:
            return ProviderResult(text, "mock", model, input_tokens, output_tokens, "stop")
        chunks = re.findall(r"\S+\s*", text) or [text]
        for chunk in chunks:
            if chunk_ms:
                time.sleep(chunk_ms / 1000)
            on_chunk(chunk)
        return ProviderResult(text, "mock", model, input_tokens, output_tokens, "stop")

    @staticmethod
    def _request(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            raise ProviderError("provider endpoint must use HTTP or HTTPS")
        retries = int(os.getenv("AGENTOPS_PROVIDER_RETRIES", "2"))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            request = urllib.request.Request(  # noqa: S310 -- scheme restricted above.
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # nosec B310
                    return json.loads(response.read())
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if not _retryable(error) or attempt == retries:
                    break
                time.sleep(_backoff_delay(error, attempt))
        raise ProviderError(f"provider request failed: {last_error}") from last_error

    @staticmethod
    def _stream_request(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        extract: Callable[[dict[str, Any]], str],
        on_chunk: Callable[[str], None],
        server_sent_events: bool = False,
    ) -> str:
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            raise ProviderError("provider endpoint must use HTTP or HTTPS")
        retries = int(os.getenv("AGENTOPS_PROVIDER_RETRIES", "2"))
        chunks: list[str] = []
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            request = urllib.request.Request(  # noqa: S310 -- scheme restricted above.
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # nosec B310
                    for raw_line in response:
                        line = raw_line.decode().strip()
                        if server_sent_events:
                            if not line.startswith("data:"):
                                continue
                            line = line.removeprefix("data:").strip()
                            if line == "[DONE]":
                                break
                        if not line:
                            continue
                        chunk = extract(json.loads(line))
                        if chunk:
                            chunks.append(chunk)
                            on_chunk(chunk)
                last_error = None
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if not _retryable(error) or attempt == retries:
                    break
                chunks.clear()
                time.sleep(_backoff_delay(error, attempt))
        if last_error is not None and not isinstance(last_error, json.JSONDecodeError):
            raise ProviderError(f"provider stream failed: {last_error}") from last_error
        return "".join(chunks)
